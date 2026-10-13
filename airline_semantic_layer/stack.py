"""CDK stack: AppSync semantic layer over DynamoDB and a Lambda that reads S3."""

import hashlib
import json
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    Expiration,
    RemovalPolicy,
    Stack,
    aws_appsync as appsync,
    aws_dynamodb as dynamodb,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    custom_resources as cr,
)
from constructs import Construct

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ONTIME_PREFIX = "ontime/"


def _to_dynamodb_item(route: dict) -> dict:
    """Convert a plain dict to DynamoDB attribute-value format."""
    item = {}
    for key, value in route.items():
        if isinstance(value, bool):
            item[key] = {"BOOL": value}
        elif isinstance(value, (int, float)):
            item[key] = {"N": str(value)}
        else:
            item[key] = {"S": str(value)}
    return item


class AirlineOntimeStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------------------------------------------------------------
        # S3: historical flight records, partitioned by month
        # ---------------------------------------------------------------
        bucket = s3.Bucket(
            self,
            "OnTimeDataBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        s3deploy.BucketDeployment(
            self,
            "OnTimeDataUpload",
            sources=[s3deploy.Source.asset(str(DATA_DIR / "ontime"))],
            destination_bucket=bucket,
            destination_key_prefix=ONTIME_PREFIX,
        )

        # ---------------------------------------------------------------
        # DynamoDB: route reference data
        # ---------------------------------------------------------------
        # An explicit name keeps the seed call below free of CloudFormation
        # tokens in map keys. The table is destroyed with the stack.
        routes_table_name = f"{self.stack_name}-routes"
        table = dynamodb.Table(
            self,
            "RoutesTable",
            table_name=routes_table_name,
            partition_key=dynamodb.Attribute(
                name="routeId", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        table.add_global_secondary_index(
            index_name="byCarrier",
            partition_key=dynamodb.Attribute(
                name="carrier", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="routeId", type=dynamodb.AttributeType.STRING
            ),
        )

        # Seed the routes on deploy (BatchWriteItem allows up to 25 items).
        routes = json.loads((DATA_DIR / "routes.json").read_text())
        if len(routes) > 25:
            raise ValueError("Seeding supports up to 25 routes per BatchWriteItem")
        routes_hash = hashlib.sha256(
            json.dumps(routes, sort_keys=True).encode()
        ).hexdigest()[:12]
        seed_call = cr.AwsSdkCall(
            service="DynamoDB",
            action="batchWriteItem",
            parameters={
                "RequestItems": {
                    routes_table_name: [
                        {"PutRequest": {"Item": _to_dynamodb_item(r)}} for r in routes
                    ]
                }
            },
            physical_resource_id=cr.PhysicalResourceId.of(f"routes-seed-{routes_hash}"),
        )
        seed = cr.AwsCustomResource(
            self,
            "SeedRoutes",
            on_create=seed_call,
            on_update=seed_call,
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=[table.table_arn]
            ),
            install_latest_aws_sdk=False,
        )
        seed.node.add_dependency(table)

        # ---------------------------------------------------------------
        # Lambda: calculates performance metrics from S3
        # ---------------------------------------------------------------
        perf_logs = logs.LogGroup(
            self,
            "PerformanceFnLogs",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        perf_fn = _lambda.Function(
            self,
            "PerformanceFn",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="handler.handler",
            code=_lambda.Code.from_asset(str(ROOT / "lambda" / "performance")),
            memory_size=512,
            timeout=Duration.seconds(15),
            environment={
                "DATA_BUCKET": bucket.bucket_name,
                "DATA_PREFIX": ONTIME_PREFIX,
            },
            log_group=perf_logs,
            description="Calculates route on-time performance from S3 flight records",
        )
        bucket.grant_read(perf_fn)

        # ---------------------------------------------------------------
        # AppSync: the semantic layer
        # ---------------------------------------------------------------
        api = appsync.GraphqlApi(
            self,
            "SemanticLayerApi",
            name="airline-ontime-semantic-layer",
            definition=appsync.Definition.from_file(
                str(ROOT / "graphql" / "schema.graphql")
            ),
            authorization_config=appsync.AuthorizationConfig(
                default_authorization=appsync.AuthorizationMode(
                    authorization_type=appsync.AuthorizationType.API_KEY,
                    api_key_config=appsync.ApiKeyConfig(
                        name="demo-key",
                        description="Demo key for the on-time semantic layer",
                        expires=Expiration.after(Duration.days(30)),
                    ),
                )
            ),
            log_config=appsync.LogConfig(field_log_level=appsync.FieldLogLevel.ERROR),
        )

        routes_ds = api.add_dynamo_db_data_source("RoutesDataSource", table)
        perf_ds = api.add_lambda_data_source("PerformanceDataSource", perf_fn)

        routes_ds.create_resolver(
            "QueryRouteResolver",
            type_name="Query",
            field_name="route",
            runtime=appsync.FunctionRuntime.JS_1_0_0,
            code=appsync.Code.from_asset(str(ROOT / "resolvers" / "getRoute.js")),
        )
        routes_ds.create_resolver(
            "QueryRoutesResolver",
            type_name="Query",
            field_name="routes",
            runtime=appsync.FunctionRuntime.JS_1_0_0,
            code=appsync.Code.from_asset(str(ROOT / "resolvers" / "listRoutes.js")),
        )
        # No mapping templates or code -> direct Lambda resolver: AppSync sends
        # arguments plus the parent Route (source) straight to the function.
        perf_ds.create_resolver(
            "RoutePerformanceResolver",
            type_name="Route",
            field_name="performance",
        )

        # ---------------------------------------------------------------
        # Outputs
        # ---------------------------------------------------------------
        CfnOutput(self, "GraphQLApiUrl", value=api.graphql_url)
        CfnOutput(self, "GraphQLApiKey", value=api.api_key or "")
        CfnOutput(self, "GraphQLApiId", value=api.api_id)
        CfnOutput(self, "DataBucketName", value=bucket.bucket_name)
        CfnOutput(self, "RoutesTableName", value=table.table_name)
        CfnOutput(self, "PerformanceFunctionName", value=perf_fn.function_name)
