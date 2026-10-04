"""Public endpoints taken from the official docs. No authenticated routes."""

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
WS_MARKET = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# docs.polymarket.com/asyncapi.json SubscriptionRequest.assets_ids has no maxItems.
# docs.polymarket.com/market-data/websocket/overview likewise states no cap.
WS_ASSETS_PER_CONNECTION_DOCUMENTED: int | None = None
