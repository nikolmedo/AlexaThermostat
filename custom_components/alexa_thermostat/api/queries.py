"""GraphQL documents sent to https://alexa.<domain>/nexus/v1/graphql."""

from __future__ import annotations

GRAPHQL_PATH = "/nexus/v1/graphql"

ENDPOINTS_QUERY = """
query Endpoints {
  endpoints {
    items {
      id
      friendlyName
      enablement
      displayCategories { primary { value } }
      serialNumber { value { text } }
      model { value { text } }
      manufacturer { value { text } }
      features { name instance operations { name } }
    }
  }
}
"""

THERMOSTAT_STATE_QUERY = """
query getThermostatStates($endpointId: String!) {
  endpoint(id: $endpointId) {
    features {
      name
      instance
      properties {
        name
        ... on RangeValue { rangeValue { value } }
        ... on Setpoint { value { value scale } }
        ... on TemperatureSensor { value { value scale } }
        ... on ThermostatMode { thermostatModeValue }
        ... on Power { powerStateValue }
      }
      configuration {
        ... on RangeConfiguration { friendlyName { value { text } } }
      }
    }
  }
}
"""

SET_ENDPOINT_FEATURES_MUTATION = """
mutation setEndpointFeatures($featureControlRequests: [FeatureControlRequest!]!) {
  setEndpointFeatures(
    setEndpointFeaturesInput: { featureControlRequests: $featureControlRequests }
  ) {
    featureControlResponses { endpointId featureOperationName }
    errors { endpointId code }
  }
}
"""
