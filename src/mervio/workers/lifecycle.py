"""Cycle de vie du processus worker (Mission 004.3).

    STARTING --> READY <--> BUSY
       |           |  \\      |  \\
       |           |   DEGRADED  |
       |           v       |     v
       +------> DRAINING <-+-----+
       |           |
       v           v
    STOPPED     STOPPED
    (tout etat non terminal) --> FORCED

- STARTING   configuration lue, base et identite en cours d'etablissement
- READY      pret a prendre un travail
- BUSY       un travail en cours (un seul par processus en 004.3)
- DEGRADED   base injoignable, le worker reessaie; aucun travail pris
- DRAINING   arret demande: plus aucune prise, le travail en cours se termine
- STOPPED    arret propre (terminal)
- FORCED     arret force: delai de grace depasse ou second signal (terminal)

Une transition hors de ce graphe est une erreur de programmation: elle leve
`IllegalTransition`, jamais silencieusement ignoree.
"""
from __future__ import annotations

import threading
from enum import Enum
from typing import Callable, Dict, FrozenSet, List, Optional

from ..observability.health import WorkerState


class ProcessState(str, Enum):
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    DEGRADED = "degraded"
    DRAINING = "draining"
    STOPPED = "stopped"
    FORCED = "forced"


TERMINAL = frozenset({ProcessState.STOPPED, ProcessState.FORCED})

TRANSITIONS: Dict[ProcessState, FrozenSet[ProcessState]] = {
    ProcessState.STARTING: frozenset({ProcessState.READY, ProcessState.DRAINING, ProcessState.STOPPED,
                                      ProcessState.FORCED}),
    ProcessState.READY: frozenset({ProcessState.BUSY, ProcessState.DEGRADED, ProcessState.DRAINING,
                                   ProcessState.FORCED}),
    ProcessState.BUSY: frozenset({ProcessState.READY, ProcessState.DEGRADED, ProcessState.DRAINING,
                                  ProcessState.FORCED}),
    ProcessState.DEGRADED: frozenset({ProcessState.READY, ProcessState.DRAINING, ProcessState.FORCED}),
    ProcessState.DRAINING: frozenset({ProcessState.STOPPED, ProcessState.FORCED}),
    ProcessState.STOPPED: frozenset(),
    ProcessState.FORCED: frozenset(),
}

#: Etat publie dans le fichier de sante pour chaque etat du processus.
HEALTH_STATE = {state: WorkerState(state.value) for state in ProcessState}


class IllegalTransition(RuntimeError):
    def __init__(self, current: ProcessState, target: ProcessState) -> None:
        self.current, self.target = current, target
        super().__init__(f"transition interdite: {current.value} -> {target.value}")


class Lifecycle:
    """Etat courant du processus, partage entre fils; chaque changement est notifie."""

    def __init__(self, on_change: Optional[Callable[[ProcessState, ProcessState], None]] = None) -> None:
        self._state = ProcessState.STARTING
        self._lock = threading.RLock()
        self._on_change = on_change
        self.history: List[ProcessState] = [ProcessState.STARTING]

    @property
    def state(self) -> ProcessState:
        with self._lock:
            return self._state

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def can(self, target: ProcessState) -> bool:
        with self._lock:
            return ProcessState(target) in TRANSITIONS[self._state]

    def transition(self, target: ProcessState) -> ProcessState:
        target = ProcessState(target)
        with self._lock:
            previous = self._state
            if target not in TRANSITIONS[previous]:
                raise IllegalTransition(previous, target)
            self._state = target
            self.history.append(target)
            if self._on_change is not None:
                self._on_change(previous, target)
            return previous

    def transition_if_allowed(self, target: ProcessState) -> bool:
        """Transition si elle est permise depuis l'etat courant (atomique). Faux sinon."""
        with self._lock:
            if ProcessState(target) not in TRANSITIONS[self._state]:
                return False
            self.transition(target)
            return True


__all__ = ["HEALTH_STATE", "IllegalTransition", "Lifecycle", "ProcessState", "TERMINAL", "TRANSITIONS"]
