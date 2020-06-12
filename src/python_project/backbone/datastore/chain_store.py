import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Tuple, Optional, Set

import cachetools
from python_project.backbone.block import PlexusBlock
from python_project.backbone.datastore.utils import (
    shorten,
    ranges,
    expand_ranges,
    Links,
    decode_raw,
    encode_raw,
    Ranges,
    ShortKey,
)


@dataclass
class Frontier:
    terminal: Links
    holes: Ranges
    inconsistencies: Links

    def to_bytes(self) -> bytes:
        return encode_raw(
            {"t": self.terminal, "h": self.holes, "i": self.inconsistencies}
        )

    @classmethod
    def from_bytes(cls, bytes_frontier: bytes):
        front_dict = decode_raw(bytes_frontier)
        return cls(front_dict.get("t"), front_dict.get("h"), front_dict.get("i"))


@dataclass
class FrontierDiff:
    missing: Ranges
    conflicts: Links

    def to_bytes(self) -> bytes:
        return encode_raw({"m": self.missing, "c": self.conflicts})

    @classmethod
    def from_bytes(cls, bytes_frontier: bytes):
        val_dict = decode_raw(bytes_frontier)
        return cls(val_dict.get("m"), val_dict.get("c"))


class BaseChain(ABC):
    @abstractmethod
    def add_block(self, block: PlexusBlock) -> None:
        pass

    @abstractmethod
    def reconcile(self, frontier: Frontier) -> FrontierDiff:
        pass

    @property
    @abstractmethod
    def frontier(self) -> Frontier:
        pass


class Chain(BaseChain):
    def __init__(self, is_personal_chain=False, cache_num=100_000):
        """DAG-Chain of one community based on in-memory dicts.

        Args:
            is_personal_chain: if the chain must follow personal links (previous). Default: False
            cache_num: to store and support terminal calculation. Default= 100`000
        """
        self.personal = is_personal_chain

        # Internal chain store of short hashes
        self.versions = dict()
        # Pointers to forward blocks
        self.forward_pointers = dict()
        # Known data structure inconsistencies
        self.inconsistencies = set()
        # Unknown blocks in the data structure
        self.holes = set()
        # Current terminal nodes in the DAG
        self.terminal = Links(((0, ShortKey("30303030")),))

        self.max_known_seq_num = 0
        # Cache to speed up bfs on links
        self.term_cache = cachetools.LRUCache(cache_num)

        self.lock = threading.Lock()

    def get_next_link(self, link: Tuple[int, ShortKey]) -> Optional[Links]:
        """Get forward link from the point.

        Args:
            link: tuple of sequence number and short hash key

        Returns:
            A tuple of links
        """
        val = self.forward_pointers.get(link)
        return Links(tuple(val)) if val else None

    def _update_holes(self, block_seq_num: int, block_links: Links) -> None:
        """Fix known holes, or add any new"""
        # Check if this block fixes known holes
        if block_seq_num in self.holes:
            self.holes.remove(block_seq_num)

        # Check if block introduces new holes
        for s, h in block_links:
            if s not in self.versions:
                while s not in self.versions and s >= 1:
                    self.holes.add(s)
                    s -= 1

    def _update_inconsistencies(self, block_links: Links, block_seq_num: int, block_hash: ShortKey) -> None:
        """Fix any inconsistencies in the data structure, and verify any new"""

        # Check if block introduces new inconsistencies
        for seq, hash_val in block_links:
            if seq in self.versions and hash_val not in self.versions[seq]:
                self.inconsistencies.add((seq, hash_val))

        # Check if block fixes some inconsistencies
        if (block_seq_num, block_hash) in self.inconsistencies:
            self.inconsistencies.remove((block_seq_num, block_hash))

    def __calc_terminal(self, current: Links) -> Set[Tuple[int, ShortKey]]:
        """Recursive iteration through the block links"""
        terminal = set()
        for blk_link in current:
            if blk_link not in self.forward_pointers:
                # Terminal nodes achieved
                terminal.add(blk_link)
            else:
                # Next blocks are available, check if there is cache
                cached_next = self.term_cache.get(blk_link, default=None)
                if cached_next:
                    # Cached next exits
                    new_cache = None

                    for cached_val in cached_next:
                        term_next = self.get_next_link(cached_val)
                        if not term_next:
                            # This is terminal node - update
                            terminal.update(cached_next)
                        else:
                            # This is not terminal, make next step and invalidate the cache
                            new_val = self.__calc_terminal(term_next)
                            if not new_cache:
                                new_cache = set()
                            new_cache.update(new_val)
                            terminal.update(new_val)
                    if new_cache:
                        self.term_cache[blk_link] = new_cache
                else:
                    # No cache, make step and update cache
                    next_blk = self.get_next_link(blk_link)
                    new_term = self.__calc_terminal(next_blk)
                    self.term_cache[blk_link] = new_term
                    terminal.update(new_term)
        return terminal

    # noinspection PyTypeChecker
    def _update_terminal(self, block_seq_num: int, block_short_hash: ShortKey) -> None:
        """Update current terminal nodes wrt new block"""

        # Check if the terminal nodes changed
        current_links = Links(((block_seq_num, block_short_hash),))
        # Start traversal from the block
        new_term = self.__calc_terminal(current_links)
        # Traversal from the current terminal nodes. Block can change the current terminal
        new_term.update(self.__calc_terminal(self.terminal))
        new_term = sorted(new_term)
        self.terminal = Links(tuple(new_term))

    def _update_forward_pointers(self, block_links: Links, block_seq_num: int, block_hash: ShortKey) -> None:
        for seq, hash_val in block_links:
            if (seq, hash_val) not in self.forward_pointers:
                self.forward_pointers[(seq, hash_val)] = set()
            self.forward_pointers[(seq, hash_val)].add((block_seq_num, block_hash))

    def _update_versions(self, block_seq_num: int, block_hash: ShortKey) -> None:
        if block_seq_num not in self.versions:
            self.versions[block_seq_num] = set()
            if block_seq_num > self.max_known_seq_num:
                self.max_known_seq_num = block_seq_num
        self.versions[block_seq_num].add(block_hash)

    def add_block(self, block: PlexusBlock) -> None:
        block_links = block.previous if self.personal else block.links
        block_seq_num = block.sequence_number if self.personal else block.com_seq_num
        block_hash = shorten(block.hash)

        with self.lock:
            # 1. Update versions
            self._update_versions(block_seq_num, block_hash)
            # 2. Update forward pointers
            self._update_forward_pointers(block_links, block_seq_num, block_hash)
            # 3. Update holes
            self._update_holes(block_seq_num, block_links)
            # 4. Update inconsistencies
            self._update_inconsistencies(block_links, block_seq_num, block_hash)
            # 5. Update terminal nodes
            self._update_terminal(block_seq_num, block_hash)

    @property
    def frontier(self) -> Frontier:
        with self.lock:
            return Frontier(self.terminal, ranges(self.holes), Links(tuple(sorted(self.inconsistencies))))

    def reconcile(self, frontier: Frontier) -> FrontierDiff:

        f_holes = expand_ranges(frontier.holes)
        max_term_seq = max(frontier.terminal)[0]

        front_known_seq = expand_ranges(Ranges(((1, max_term_seq),))) - f_holes
        peer_known_seq = expand_ranges(Ranges(((1, self.max_known_seq_num),))) - self.holes

        # External frontier has blocks that peer is missing => Request from front these blocks
        f_diff = front_known_seq - peer_known_seq
        missing = ranges(f_diff)

        # Front has blocks with conflicting hash => Request these blocks
        conflicts = {
            (s, h)
            for s, h in frontier.terminal
            if s in self.versions and h not in self.versions[s]
        }

        for i in self.inconsistencies:
            for t in self.__calc_terminal(Links((i,))):
                if t in frontier.terminal and t not in frontier.inconsistencies and t[0] not in frontier.holes:
                    conflicts.add(i)

        conflicts = Links(tuple(conflicts))

        return FrontierDiff(missing, conflicts)
