"""Moved to agentos.ops.backup.s3. This path is the same module for one release."""

from agentos.core.moved import alias
from agentos.ops.backup.s3 import *

alias(__name__, "agentos.ops.backup.s3")
