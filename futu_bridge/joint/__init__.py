"""Research, native ML/RL and artificial simulation over fixed local data providers.

Optional framework runtimes are isolated in child processes. Importing this
package neither imports those frameworks nor connects to a market-data source.
"""

from .contracts import Bar, Dataset, Probability
from .session import JointResearch, RuntimePaths

__all__ = ["Bar", "Dataset", "JointResearch", "Probability", "RuntimePaths"]
