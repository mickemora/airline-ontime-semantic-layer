#!/usr/bin/env python3
import aws_cdk as cdk

from airline_semantic_layer.stack import AirlineOntimeStack

app = cdk.App()
AirlineOntimeStack(
    app,
    "AirlineOntimeSemanticLayer",
    description="AppSync semantic layer over DynamoDB route data and S3 on-time records",
)
cdk.Tags.of(app).add("project", "airline-ontime-semantic-layer")
app.synth()
