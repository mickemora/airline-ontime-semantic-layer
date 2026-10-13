"""CDK assertions for the stack. Requires aws-cdk-lib (pip install -r requirements.txt)."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import aws_cdk as cdk
    from aws_cdk.assertions import Match, Template

    from airline_semantic_layer.stack import AirlineOntimeStack

    HAVE_CDK = True
except ImportError:
    HAVE_CDK = False


@unittest.skipUnless(HAVE_CDK, "aws-cdk-lib not installed")
class StackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = cdk.App()
        stack = AirlineOntimeStack(app, "TestStack")
        cls.t = Template.from_stack(stack)

    def test_appsync_api_with_api_key(self):
        self.t.resource_count_is("AWS::AppSync::GraphQLApi", 1)
        self.t.has_resource_properties(
            "AWS::AppSync::GraphQLApi", {"AuthenticationType": "API_KEY"}
        )
        self.t.resource_count_is("AWS::AppSync::ApiKey", 1)

    def test_three_resolvers(self):
        self.t.resource_count_is("AWS::AppSync::Resolver", 3)
        self.t.has_resource_properties(
            "AWS::AppSync::Resolver", {"TypeName": "Route", "FieldName": "performance"}
        )

    def test_routes_table_with_carrier_index(self):
        self.t.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "TableName": "TestStack-routes",
                "GlobalSecondaryIndexes": Match.array_with(
                    [Match.object_like({"IndexName": "byCarrier"})]
                ),
            },
        )

    def test_lambda_reads_bucket(self):
        self.t.has_resource_properties(
            "AWS::Lambda::Function",
            {
                "Handler": "handler.handler",
                "Runtime": "python3.13",
                "Environment": {
                    "Variables": Match.object_like({"DATA_PREFIX": "ontime/"})
                },
            },
        )

    def test_everything_is_destroyable(self):
        for resource in ("AWS::S3::Bucket", "AWS::DynamoDB::Table"):
            self.t.has_resource(resource, {"DeletionPolicy": "Delete"})


if __name__ == "__main__":
    unittest.main()
