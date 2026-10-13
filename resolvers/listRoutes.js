// Query.routes -> DynamoDB Query on the byCarrier index when a carrier is
// given, otherwise a Scan (the routes table is small reference data).
import { util } from '@aws-appsync/utils';

export function request(ctx) {
  const { carrier } = ctx.args;
  if (carrier) {
    return {
      operation: 'Query',
      index: 'byCarrier',
      query: {
        expression: '#carrier = :carrier',
        expressionNames: { '#carrier': 'carrier' },
        expressionValues: util.dynamodb.toMapValues({ ':carrier': carrier.toUpperCase() }),
      },
    };
  }
  return { operation: 'Scan' };
}

export function response(ctx) {
  if (ctx.error) {
    util.error(ctx.error.message, ctx.error.type);
  }
  return ctx.result.items;
}
