"""Global application-wide signaling mechanism that extensions can use to
communicate with each other in a coordinated manner without needing to import
each other's API.
"""

from blinker import NamedSignal, Signal
from typing import Optional


#: Logger that will be used to log unexpected exceptions from signal handlers
log = None

#: Namespace containing all the signals registered in this extension
signals = None


class ProtectedSignal(NamedSignal):
    """Object that is mostly API-compatible with a standard Signal_ from the
    ``blinker`` module but shields the listeners from exceptions thrown from
    another listener.
    """

    def send(self, *sender, **kwargs):
        if len(sender) == 0:
            sender = None
        elif len(sender) > 1:
            raise TypeError(
                f"send() accepts only one positional argument, {len(sender)} given"
            )
        else:
            sender = sender[0]

        result = []
        if not self.receivers:
            return result

        for receiver in self.receivers_for(sender):
            try:
                retval = receiver(sender, **kwargs)
            except Exception as ex:
                log.exception("Unexpected exception caught in signal dispatch")
                retval = ex
            result.append((receiver, retval))

        return result


class Namespace(dict):
    """A mapping of signal names to signals."""

    def signal(self, name: str, doc: Optional[str] = None) -> ProtectedSignal:
        """Return the ProtectedSignal_ called *name*, creating it if required.

        Repeated calls to this function will return the same signal object.
        """
        try:
            return self[name]
        except KeyError:
            return self.setdefault(name, ProtectedSignal(name, doc))


def get_signal(name: str) -> Signal:
    """Returns the signal with the given name, registering it on-the-fly if
    needed.

    Parameters:
        name: the name of the signal

    Returns:
        the signal associated to the given name
    """
    global signals

    if signals is None:
        raise RuntimeError(
            "Attempted to get a signal reference when the extension is not running"
        )

    return signals.signal(name)


def load(app, configuration, logger):
    global signals
    global log

    log = logger
    signals = Namespace()


def unload():
    global signals
    signals = None


#: The API of this extension
exports = {"get": get_signal}