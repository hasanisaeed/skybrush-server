"""Classes and functions related to MAVLink networks, i.e. a set of MAVLink
connections over which the system IDs of the devices share the same namespace.

For instance, a Skybrush server may participate in a MAVLink network with two
connections: a wifi connection and a fallback radio connection. The system ID
of a MAVLink message received on either of these two connections refers to the
same device. However, the same system ID in a different MAVLink network may
refer to a completely different device. The introduction of the concept of
MAVLink networks in the Skybrush server will allow us in the future to manage
multiple independent MAVLink-based drone swarms.
"""

from collections import defaultdict
from contextlib import contextmanager, ExitStack
from time import time_ns
from trio.abc import ReceiveChannel
from trio_util import periodic
from typing import Any, Callable, Optional, Sequence, Tuple, Union

from flockwave.connections import Connection, create_connection, ListenerConnection
from flockwave.server.comm import CommunicationManager
from flockwave.server.concurrency import Future, race
from flockwave.server.model import ConnectionPurpose, UAV
from flockwave.server.utils import nop, overridden

from .comm import create_communication_manager, MAVLinkMessage
from .driver import MAVLinkUAV
from .enums import MAVAutopilot, MAVComponent, MAVMessageType, MAVState, MAVType
from .packets import DroneShowStatus
from .rtk import RTKCorrectionPacketEncoder
from .types import (
    MAVLinkMessageMatcher,
    MAVLinkMessageSpecification,
    MAVLinkNetworkSpecification,
)
from .utils import log_level_from_severity, log_id_from_message

__all__ = ("MAVLinkNetwork",)

DEFAULT_NAME = ""

#: MAVLink message specification for heartbeat messages that we are sending
#: to connected UAVs to keep them sending telemetry data
HEARTBEAT_SPEC = (
    "HEARTBEAT",
    {
        "type": MAVType.GCS,
        "autopilot": MAVAutopilot.INVALID,
        "base_mode": 0,
        "custom_mode": 0,
        "system_status": MAVState.STANDBY,
    },
)


class MAVLinkNetwork:
    """Representation of a MAVLink network."""

    @classmethod
    def from_specification(cls, spec: MAVLinkNetworkSpecification):
        """Creates a MAVLink network from its specification, typically found in
        a configuration file.
        """
        result = cls(
            spec.id,
            system_id=spec.system_id,
            id_formatter=spec.id_format.format,
            packet_loss=spec.packet_loss,
        )

        for index, connection_spec in enumerate(spec.connections):
            connection = create_connection(connection_spec)
            result.add_connection(connection)

        return result

    def __init__(
        self,
        id: str,
        *,
        system_id: int = 255,
        id_formatter: Callable[[int, str], str] = "{0}".format,
        packet_loss: float = 0,
    ):
        """Constructor.

        Creates a new MAVLink network with the given network ID. Network
        identifiers must be unique in the Skybrush server.

        Parameters:
            id: the network ID
            system_id: the MAVLink system ID of the Skybrush server within the
                network
            id_formatter: function that can be called with a MAVLink system ID
                and the network ID, and that must return a string that will be
                used for the drone with the given system ID on the network
            packet_loss: when larger than zero, simulates packet loss on the
                network by randomly dropping received and sent MAVLink messages
        """
        self._id = id
        self._id_formatter = id_formatter
        self._matchers = None
        self._packet_loss = max(float(packet_loss), 0.0)
        self._system_id = 255

        self._connections = []
        self._uavs = {}
        self._uav_addresses = {}

        self._rtk_correction_packet_encoder = RTKCorrectionPacketEncoder()

    def add_connection(self, connection: Connection):
        """Adds the given connection object to this network.

        Parameters:
            connection: the connection to add
        """
        self._connections.append(connection)

    @contextmanager
    def expect_packet(
        self,
        type: Union[int, str, MAVMessageType],
        params: MAVLinkMessageMatcher = None,
        system_id: Optional[int] = None,
    ) -> Future[MAVLinkMessage]:
        """Sets up a handler that waits for a MAVLink packet of a given type,
        optionally matching its content with the given parameter values based
        on strict equality.

        Parameters:
            type: the type of the MAVLink message to wait for
            params: dictionary mapping parameter names to the values that we
                expect to see in the matched packet, or a callable that
                receives a MAVLinkMessage and returns `True` if it matches the
                packet we are looking for
            system_id: the system ID of the sender of the message; `None` means
                any system ID

        Returns:
            a Future that resolves to the next MAVLink message that matches the
            given type and parameter values.
        """
        type_str = type if isinstance(type, str) else MAVMessageType(type).name

        # Map values of type 'bytes' to 'str' in the params dict because
        # pymavlink never returns 'bytes'
        if not callable(params):
            for name, value in params.items():
                if isinstance(value, bytes):
                    try:
                        params[name] = value.decode("utf-8")
                    except ValueError:
                        pass

        future = Future()
        item = (system_id, params, future)
        matchers = self._matchers[type_str]

        matchers.append(item)
        try:
            yield future
        finally:
            matchers.pop(matchers.index(item))

    @property
    def id(self) -> str:
        """The unique identifier of this MAVLink network."""
        return self._id

    async def run(self, *, driver, log, register_uav, supervisor, use_connection):
        """Starts the network manager.

        Parameters:
            driver: the driver object for MAVLink-based drones
            log: a logging object where the network manager can log messages
            register_uav: a callable that can be called with a single UAV_
                object as an argument to get it registered in the application
            supervisor: the application supervisor that can be used to re-open
                connections if they get closed
            use_connection: context manager that must be entered when the
                network manager wishes to register a connection in the
                application
        """
        if len(self._connections) > 1:
            if self.id:
                id_format = "{0}/{1}"
            else:
                id_format = "{1}"
        else:
            id_format = "{0}"

        # Register the communication links
        with ExitStack() as stack:
            for index, connection in enumerate(self._connections):
                full_id = id_format.format(self.id, index)
                description = (
                    "MAVLink listener"
                    if isinstance(connection, ListenerConnection)
                    else "MAVLink connection"
                )
                if full_id:
                    description += f" ({full_id})"

                stack.enter_context(
                    use_connection(
                        connection,
                        f"MAVLink: {full_id}" if full_id else "MAVLink",
                        description=description,
                        purpose=ConnectionPurpose.uavRadioLink,
                    )
                )

            # Create the communication manager
            manager = create_communication_manager(packet_loss=self._packet_loss)

            # Warn the user about the simulated packet loss setting
            if self._packet_loss > 0:
                percentage = round(min(1, self._packet_loss) * 100)
                log.warn(
                    f"Simulating {percentage}% packet loss on MAVLink network {self._id}"
                )

            # Register the links with the communication manager. The order is
            # important here; the ones coming first will primarily be used for
            # sending, falling back to later ones if sending on the first one
            # fails
            for index, connection in enumerate(self._connections):
                manager.add(connection, name=DEFAULT_NAME)

            # Set up a dictionary that will map from MAVLink message types that
            # we are waiting for to lists of corresponding (predicate, future)
            # pairs
            matchers = defaultdict(list)

            # Override some of our properties with the values we were called with
            stack.enter_context(
                overridden(
                    self,
                    log=log,
                    driver=driver,
                    manager=manager,
                    register_uav=register_uav,
                    _matchers=matchers,
                )
            )

            # Start the communication manager
            try:
                await manager.run(
                    consumer=self._handle_inbound_messages,
                    supervisor=supervisor,
                    log=log,
                    tasks=[self._generate_heartbeats],
                )
            finally:
                for matcher in matchers.values():
                    for _, _, future in matcher:
                        future.cancel()

    async def broadcast_packet(self, spec: MAVLinkMessageSpecification) -> None:
        """Broadcasts a message to all UAVs in the network.

        Parameters:
            spec: the specification of the MAVLink message to send
        """
        await self.manager.broadcast_packet(spec)

    def enqueue_rtk_correction_packet(self, packet: bytes) -> None:
        """Handles an RTK correction packet that the server wishes to forward
        to the drones in this network.

        Parameters:
            packet: the raw RTK correction packet to forward to the drones in
                this network
        """
        if not self.manager:
            return

        for message in self._rtk_correction_packet_encoder.encode(packet):
            self.manager.enqueue_broadcast_packet(message, allow_failure=True)

    def notify_start_method_changed(self, config):
        """Notifies the network that the automatic start configuration of the
        drones has changed in the system. The network will then update the
        start configuration of each drone.
        """
        pass

    async def send_heartbeat(self, target: UAV) -> Optional[MAVLinkMessage]:
        """Sends a heartbeat targeted to the given UAV.

        It is assumed (and not checked) that the UAV belongs to this network.

        Parameters:
            target: the UAV to send the heartbeat to
        """
        spec = HEARTBEAT_SPEC
        address = self._uav_addresses.get(target)
        if address is None:
            raise RuntimeError("UAV has no address in this network")

        destination = (DEFAULT_NAME, address)
        await self.manager.send_packet(spec, destination)

    async def send_packet(
        self,
        spec: MAVLinkMessageSpecification,
        target: UAV,
        wait_for_response: Optional[Tuple[str, MAVLinkMessageMatcher]] = None,
        wait_for_one_of: Optional[Sequence[Tuple[str, MAVLinkMessageMatcher]]] = None,
    ) -> Optional[MAVLinkMessage]:
        """Sends a message to the given UAV and optionally waits for a matching
        response.

        It is assumed (and not checked) that the UAV belongs to this network.

        Parameters:
            spec: the specification of the MAVLink message to send
            target: the UAV to send the message to
            wait_for_response: when not `None`, specifies a MAVLink message
                type to wait for as a response, and an additional message
                matcher that examines the message further to decide whether this
                is really the response we are interested in. The matcher may be
                `None` to match all messages of the given type, a dictionary
                mapping MAVLink field names to expected values, or a callable
                that gets called with the retrieved MAVLink message of the
                given type and must return `True` if and only if the message
                matches our expectations. The source system of the MAVLink
                reply must also be equal to the system ID of the UAV where
                the original message was sent.
        """
        spec[1].update(
            target_system=target.system_id,
            target_component=MAVComponent.AUTOPILOT1,
            _mavlink_version=target.mavlink_version,
        )

        address = self._uav_addresses.get(target)
        if address is None:
            raise RuntimeError("UAV has no address in this network")

        destination = (DEFAULT_NAME, address)

        if wait_for_response:
            response_type, response_fields = wait_for_response
            with self.expect_packet(
                response_type, response_fields, system_id=target.system_id
            ) as future:
                # TODO(ntamas): in theory, we could be getting a matching packet
                # _before_ we sent ours. Sort this out if it causes problems.
                await self.manager.send_packet(spec, destination)
                return await future.wait()

        elif wait_for_one_of:
            tasks = {}

            with ExitStack() as stack:
                # Prepare futures for every single message type that we expect,
                # and then send the message itself
                for key, (response_type, response_fields) in wait_for_one_of.items():
                    future = stack.enter_context(
                        self.expect_packet(
                            response_type, response_fields, system_id=target.system_id
                        )
                    )
                    tasks[key] = future.wait

                # Now send the message and wait for _any_ of the futures to
                # succeed
                await self.manager.send_packet(spec, destination)
                return await race(tasks)
        else:
            await self.manager.send_packet(spec, destination)

    def _create_uav(self, system_id: str) -> MAVLinkUAV:
        """Creates a new UAV with the given system ID in this network and
        registers it in the UAV registry.
        """
        uav_id = self._id_formatter(system_id, self.id)

        self._uavs[system_id] = uav = self.driver.create_uav(uav_id)
        uav.assign_to_network_and_system_id(self.id, system_id)

        self.register_uav(uav)

        return uav

    def _find_uav_from_message(
        self, message: MAVLinkMessage, address: Any
    ) -> Optional[UAV]:
        """Finds the UAV that this message is sent from, based on its system ID,
        creating a new UAV object if we have not seen the UAV yet.

        Parameters:
            message: the message
            address: the address that the message was sent from

        Returns:
            the UAV belonging to the system ID of the message or `None` if the
            message was a broadcast message
        """
        system_id = message.get_srcSystem()
        if system_id == 0:
            return None
        else:
            uav = self._uavs.get(system_id)
            if not uav:
                uav = self._create_uav(system_id)

            # TODO(ntamas): protect from address hijacking!
            self._uav_addresses[uav] = address

            return uav

    async def _generate_heartbeats(self, manager: CommunicationManager):
        """Generates heartbeat messages on the channels corresponding to the
        network.
        """
        async for _ in periodic(1):
            await manager.broadcast_packet(HEARTBEAT_SPEC, allow_failure=True)

    async def _handle_inbound_messages(self, channel: ReceiveChannel):
        """Handles inbound MAVLink messages from all the communication links
        that the extension manages.

        Parameters:
            channel: a Trio receive channel that yields inbound MAVLink messages.
        """
        handlers = {
            "AUTOPILOT_VERSION": self._handle_message_autopilot_version,
            "BAD_DATA": nop,
            "COMMAND_ACK": nop,
            "DATA16": self._handle_message_data16,
            "FILE_TRANSFER_PROTOCOL": nop,
            "GLOBAL_POSITION_INT": self._handle_message_global_position_int,
            "GPS_GLOBAL_ORIGIN": nop,
            "GPS_RAW_INT": self._handle_message_gps_raw_int,
            "HEARTBEAT": self._handle_message_heartbeat,
            "HOME_POSITION": nop,
            "HWSTATUS": nop,
            "LOCAL_POSITION_NED": nop,  # maybe later?
            "MEMINFO": nop,
            "MISSION_ACK": nop,  # used for mission and geofence download / upload
            "MISSION_COUNT": nop,  # used for mission and geofence download / upload
            "MISSION_CURRENT": nop,  # maybe later?
            "MISSION_ITEM_INT": nop,  # used for mission and geofence download / upload
            "MISSION_REQUEST": nop,  # used for mission and geofence download / upload
            "NAV_CONTROLLER_OUTPUT": nop,
            "PARAM_VALUE": nop,
            "POSITION_TARGET_GLOBAL_INT": nop,
            "POWER_STATUS": nop,
            "STATUSTEXT": self._handle_message_statustext,
            "SYS_STATUS": self._handle_message_sys_status,
            "TIMESYNC": self._handle_message_timesync,
        }

        autopilot_component_id = MAVComponent.AUTOPILOT1

        async for connection_id, (message, address) in channel:
            if message.get_srcComponent() != autopilot_component_id:
                # We do not handle messages from any other component but an
                # autopilot
                continue

            # Uncomment this for debugging
            # self.log.info(repr(message))

            # Get the message type
            type = message.get_type()

            # Resolve all futures that are waiting for this message
            for system_id, params, future in self._matchers[type]:
                if system_id is not None and message.get_srcSystem() != system_id:
                    matched = False
                elif callable(params):
                    matched = params(message)
                elif params is None:
                    matched = True
                else:
                    matched = all(
                        getattr(message, param_name, None) == param_value
                        for param_name, param_value in params.items()
                    )
                if matched:
                    future.set_result(message)

            # Call the message handler if we have one
            handler = handlers.get(type)
            if handler:
                try:
                    handler(message, connection_id=connection_id, address=address)
                except Exception:
                    self.log.exception(
                        f"Error while handling MAVLink message of type {type}"
                    )
            else:
                self.log.warn(
                    f"Unhandled MAVLink message type: {type}",
                    extra=self._log_extra_from_message(message),
                )
                handlers[type] = nop

    def _handle_message_autopilot_version(
        self, message: MAVLinkMessage, *, connection_id: str, address: Any
    ):
        uav = self._find_uav_from_message(message, address)
        if uav:
            uav.handle_message_autopilot_version(message)

    def _handle_message_data16(
        self, message: MAVLinkMessage, *, connection_id: str, address: Any
    ):
        if message.type == DroneShowStatus.TYPE:
            uav = self._find_uav_from_message(message, address)
            if uav:
                uav.handle_message_drone_show_status(message)

    def _handle_message_global_position_int(
        self, message: MAVLinkMessage, *, connection_id: str, address: Any
    ):
        uav = self._find_uav_from_message(message, address)
        if uav:
            uav.handle_message_global_position_int(message)

    def _handle_message_gps_raw_int(
        self, message: MAVLinkMessage, *, connection_id: str, address: Any
    ):
        uav = self._find_uav_from_message(message, address)
        if uav:
            uav.handle_message_gps_raw_int(message)

    def _handle_message_heartbeat(
        self, message: MAVLinkMessage, *, connection_id: str, address: Any
    ):
        """Handles an incoming MAVLink HEARTBEAT message."""
        if not MAVType(message.type).is_vehicle:
            # Ignore non-vehicle heartbeats
            return

        # Forward heartbeat to the appropriate UAV
        uav = self._find_uav_from_message(message, address)
        if uav:
            uav.handle_message_heartbeat(message)
            # TODO(ntamas): if the UAV requires regular heartbeat packets to be
            # sent from the GCS, uncomment this
            # self.driver.run_in_background(self.send_heartbeat, uav)

    def _handle_message_statustext(
        self, message: MAVLinkMessage, *, connection_id: str, address: Any
    ):
        """Handles an incoming MAVLink STATUSTEXT message and forwards it to the
        log console.
        """
        if message.text and message.text.startswith("PreArm: "):
            uav = self._find_uav_from_message(message, address)
            uav.notify_prearm_failure(message.text[8:])
        else:
            self.log.log(
                log_level_from_severity(message.severity),
                message.text,
                extra=self._log_extra_from_message(message),
            )

    def _handle_message_sys_status(
        self, message: MAVLinkMessage, *, connection_id: str, address: Any
    ):
        uav = self._find_uav_from_message(message, address)
        if uav:
            uav.handle_message_sys_status(message)

    def _handle_message_timesync(
        self, message: MAVLinkMessage, *, connection_id: str, address: Any
    ):
        """Handles an incoming MAVLink TIMESYNC message."""
        if message.tc1 != 0:
            now = time_ns() // 1000
            self.log.info(f"Roundtrip time: {(now - message.ts1) // 1000} msec")
        else:
            # Timesync request, ignore it.
            pass

    def _log_extra_from_message(self, message: MAVLinkMessage):
        return {"id": log_id_from_message(message, self.id)}
