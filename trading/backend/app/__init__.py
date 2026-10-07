"""ATLAS — personal AI trading and investment operating system.

Package layout:

    app.config      environment settings + validated YAML config
    app.core        logging, decision tracing, shared utilities
    app.events      the internal async event bus
    app.models      Pydantic domain models (the shared vocabulary)
    app.brokers     broker/market-data adapters (the only SDK consumers)
    app.market_data the single streaming service + indicator maths
    app.agents      the agent framework and the agents themselves
    app.risk        deterministic risk rules and the kill switch
    app.strategies  strategy modules (all disabled by default)
    app.database    SQLAlchemy models and session management
    app.api         FastAPI routes
"""

__version__ = "0.1.0"
