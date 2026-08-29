"""pzmodmanager - Project Zomboid mod compatibility checker."""

import logging

# Without a handler, warnings would leak to stderr and disturb the interface.
# The CLI replaces this with a real file handler through logs.setup_logging().
logging.getLogger("pzmodmanager").addHandler(logging.NullHandler())
