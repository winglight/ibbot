import os

# Ensure API integration tests can start the FastAPI server by default.
# Some modules set IBBOT_SKIP_SERVER=1 to opt-out for their own tests, but
# we need the server for integration suites. Setting the default to "0"
# prevents later setdefault(...) calls from flipping it to "1" globally.
os.environ.setdefault("IBBOT_SKIP_SERVER", "0")
