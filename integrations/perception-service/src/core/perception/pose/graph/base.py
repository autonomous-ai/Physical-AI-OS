"""Abstract base class for skeleton graph definitions."""

from abc import ABC, abstractmethod

import numpy as np
import numpy.typing as npt


class SkeletonGraph(ABC):
    """Base interface for skeleton topology (joint names, connectivity, adjacency)."""

    @property
    @abstractmethod
    def joint_names(self) -> dict[int, str]:
        """Maps joint index → human-readable name."""

    @property
    @abstractmethod
    def edges(self) -> list[tuple[int, int]]:
        """Bone connectivity pairs (parent, child)."""

    @property
    def name_to_joint(self) -> dict[str, int]:
        """Reverse mapping: name → joint index."""
        return {name: idx for idx, name in self.joint_names.items()}

    def joint(self, name: str) -> int:
        """Look up joint index by name. Raises KeyError if not found."""
        return self.name_to_joint[name]

    @property
    def num_joints(self) -> int:
        return len(self.joint_names)

    @property
    def adjacency_matrix(self) -> npt.NDArray[np.bool_]:
        """(N, N) symmetric adjacency matrix computed from edges."""
        N: int = self.num_joints
        adj: npt.NDArray[np.bool_] = np.zeros((N, N), dtype=np.bool_)
        for u, v in self.edges:
            adj[u, v] = True
            adj[v, u] = True
        return adj
