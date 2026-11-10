"""What this process has already proven about an archive payload, so the same bytes are not hashed again on every use.

A payload is append-only and a receipt's digest covers its whole prefix. Verifying a receipt from byte zero on every
capture and every read costs a pass over the entire transcript each time an agent writes a line, and that cost grows
with the conversation. Once this process has hashed a payload's first ``length`` bytes from disk, the hash state at
that length says everything those bytes can say; a later, longer receipt is verified by hashing only what follows.

What stays true:

* Every byte is hashed from disk by this process before anything reads it. Only the repeated hashing is saved.
* A digest is still compared for every receipt that is used. One at or below the proven length is compared with the
  digest this process computed at exactly that length; one this process never computed is verified from byte zero.
* The proof belongs to one file. A payload that is another file, or shorter than what was proven, is verified from
  byte zero.
* A proof expires. After ``full_every`` seconds the next use verifies from byte zero again, so damage to bytes that
  were already proven is found within that time instead of on the next use. A final capture never uses a proof.
* A verification that fails forgets the proof, so the next one starts from byte zero.

What changes: between two full verifications, a change to already proven bytes of a payload (which nothing in this
application makes) is not noticed. The comparison of a transcript source with its archived prefix is not part of this;
a capture still reads and compares the whole prefix every time.

The proofs live in memory only, one per lineage, and a restart begins with none.
"""
from __future__ import annotations

from collections import OrderedDict
import threading
import time

FULL_VERIFY_SECONDS = 900.0
LINEAGES = 512
MARKS = 64


class Proof:
    """The hash state after a payload's first ``length`` bytes, and the digests computed at earlier lengths."""

    __slots__ = ("identity", "length", "hasher", "marks", "verified_at")

    def __init__(self, identity, length, hasher, marks, verified_at):
        self.identity, self.length, self.hasher, self.marks, self.verified_at = identity, length, hasher, marks, verified_at


class Proofs:
    def __init__(self, *, clock=time.monotonic, full_every: float = FULL_VERIFY_SECONDS, lineages: int = LINEAGES,
                 marks: int = MARKS):
        self.clock, self.full_every, self._lineages, self._marks = clock, full_every, lineages, marks
        self._proofs: OrderedDict = OrderedDict()
        self._guard = threading.Lock()

    def resume(self, key, identity, size: int) -> Proof | None:
        """A private copy of the proof for ``key`` when it still applies to the file ``identity`` of ``size`` bytes."""
        with self._guard:
            proof = self._proofs.get(key)
            if proof is None:
                return None
            if proof.identity != identity or size < proof.length:
                del self._proofs[key]         # another file, or a shorter one: nothing proven carries over
                return None
            if self._expired(proof):
                return None                   # kept until the full verification that is now due replaces it
            self._proofs.move_to_end(key)
            return Proof(proof.identity, proof.length, proof.hasher.copy(), dict(proof.marks), proof.verified_at)

    def record(self, key, identity, length: int, hasher, marks: dict, *, continued: Proof | None = None) -> None:
        """Keep what a verification of the first ``length`` bytes just established: the hash state there, and in
        ``marks`` the digests it computed on the way.

        A pass from byte zero (``continued`` is None) starts a proof with a new lifetime, unless a longer proof that
        has not expired exists: that one stays as it is and only learns the digests. A pass that continued a proof
        extends that same proof and keeps its lifetime; when the proof was replaced meanwhile, nothing is kept."""
        with self._guard:
            proof = self._proofs.get(key)
            if proof is not None and proof.identity != identity:
                proof = None
            if continued is not None:
                if proof is None or proof.verified_at != continued.verified_at:
                    return
                if length > proof.length:
                    proof.length, proof.hasher = length, hasher.copy()
            elif proof is None or self._expired(proof) or length >= proof.length:
                proof = self._proofs[key] = Proof(identity, length, hasher.copy(), OrderedDict(), self.clock())
            for mark, digest in sorted(marks.items()):
                proof.marks[mark] = digest
                proof.marks.move_to_end(mark)
            while len(proof.marks) > self._marks:
                proof.marks.popitem(last=False)
            self._proofs.move_to_end(key)
            while len(self._proofs) > self._lineages:
                self._proofs.popitem(last=False)

    def _expired(self, proof: Proof) -> bool:
        return self.clock() - proof.verified_at >= self.full_every

    def forget(self, key=None) -> None:
        with self._guard:
            if key is None:
                self._proofs.clear()
            else:
                self._proofs.pop(key, None)


PROOFS = Proofs()
