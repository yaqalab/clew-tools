"""Clew's local analysis engine (docs/36 section 5): a translator between Clew and the researcher's
own statistics programs. Clew sends one neutral command with the data; this server turns it into
the chosen backend's own language, runs it, and returns one neutral result. Standard library only.
"""

__version__ = "0.1.0"
API_VERSION = 1
APP_NAME = "clew-analysis-server"
