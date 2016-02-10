"""Connection-related model objects."""

from __future__ import absolute_import

from flockwave.spec.schema import get_complex_object_schema, \
    get_enum_from_schema
from .metamagic import ModelMeta
from .mixins import TimestampMixin

__all__ = ("ConnectionInfo", "ConnectionPurpose", "ConnectionStatus")


ConnectionPurpose = get_enum_from_schema("connectionPurpose",
                                         "ConnectionPurpose")
ConnectionStatus = get_enum_from_schema("connectionStatus",
                                        "ConnectionStatus")


class ConnectionInfo(TimestampMixin):
    """Class representing the status information available about a single
    connection.
    """

    __metaclass__ = ModelMeta

    class __meta__:
        schema = get_complex_object_schema("connectionInfo")

    def __init__(self, id=None, timestamp=None):
        """Constructor.

        Parameters:
            id (str or None): ID of the connection
            timestamp (datetime or None): time when the last packet was
                received from the connection, or if it is not available,
                the time when the conncetion changed status the last time.
                ``None`` means to use the current date and time.
        """
        TimestampMixin.__init__(self, timestamp)
        self.id = id
        self.purpose = ConnectionPurpose.other
        self.status = ConnectionStatus.unknown