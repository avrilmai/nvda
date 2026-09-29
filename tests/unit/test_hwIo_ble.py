# A part of NonVisual Desktop Access (NVDA)
# Copyright (C) 2025-2026 NV Access Limited, Dot Incorporated, Bram Duvigneau
# This file may be used under the terms of the GNU General Public License, version 2 or later, as modified by the NVDA license.
# For full terms and any additional permissions, see the NVDA license file: https://github.com/nvaccess/nvda/blob/master/copying.txt

"""Unit tests for the hwIo.ble module.

These tests cover the BLE scanner, BLE I/O, and device discovery functionality.
"""

import asyncio  # noqa: I001
import gc
import unittest
import weakref
from queue import Queue
from threading import Event, Thread
from unittest.mock import MagicMock, patch

from bleak.backends.device import BLEDevice
from winrt.windows.devices.radios import RadioState
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakBluetoothNotAvailableError, BleakBluetoothNotAvailableReason
from hwIo.ble._scanner import Scanner, SCAN_CONTROL_TIMEOUT_SECONDS
from hwIo.ble._io import Ble, LINK_TIMEOUT_SECONDS, queueReader
from hwIo.ble import findDeviceByAddress, getDiscoveredDevice, isAvailable
from hwIo.ble import terminate as terminateBle


def _runCoroutineHere(coro, timeout=None):
	"""Run a coroutine to completion here, standing in for the asyncio event loop."""
	loop = asyncio.new_event_loop()
	try:
		return loop.run_until_complete(coro)
	finally:
		loop.close()


class TestScanner(unittest.TestCase):
	"""Tests for hwIo.ble.Scanner"""

	def setUp(self):
		"""Set up patches and create Scanner instance."""

		self.bleakScannerPatcher = patch("hwIo.ble._scanner.bleak.BleakScanner")
		self.mockBleakScannerClass = self.bleakScannerPatcher.start()

		def fakeRunCoroutineSync(coro: object, timeout: float | None = None) -> None:
			if hasattr(coro, "close"):
				coro.close()

		self.runCoroutinePatcher = patch(
			"hwIo.ble._scanner.runCoroutineSync",
			side_effect=fakeRunCoroutineSync,
		)
		self.mockRunCoroutine = self.runCoroutinePatcher.start()

		# Use regular MagicMock for start/stop: the coroutines they
		# return are immediately closed by fakeRunCoroutine without
		# being awaited, so they don't need to be awaitable.
		self.mockScannerInstance = MagicMock()
		self.mockBleakScannerClass.return_value = self.mockScannerInstance

		self.Scanner = Scanner

	def tearDown(self):
		"""Clean up patches."""

		self.runCoroutinePatcher.stop()
		self.bleakScannerPatcher.stop()

	def test_startScanning(self):
		"""Test that starting scan calls Bleak scanner and sets isScanning flag."""
		scanner = self.Scanner()

		self.assertFalse(scanner.isScanning)

		scanner.start(duration=0)

		self.mockScannerInstance.start.assert_called_once()
		self.assertTrue(scanner.isScanning)

	def _makeStartFail(self, error: Exception) -> None:
		"""Make the next call into Bleak fail with the given error."""

		def fail(coro: object, timeout: float | None = None) -> None:
			if hasattr(coro, "close"):
				coro.close()
			raise error

		self.mockRunCoroutine.side_effect = fail

	def test_startRefusedIsReported(self):
		"""A start Bleak refuses reaches the caller rather than being swallowed."""
		scanner = self.Scanner()
		self._makeStartFail(
			BleakBluetoothNotAvailableError(
				"No Bluetooth adapter found",
				BleakBluetoothNotAvailableReason.NO_BLUETOOTH,
			),
		)
		with self.assertRaises(BleakBluetoothNotAvailableError):
			scanner.start()

	def test_startRefusedLeavesScannerIdle(self):
		"""A scan that never started is not recorded as running.

		Otherwise every later attempt is skipped as redundant, and callers wait for
		results that cannot arrive.
		"""
		scanner = self.Scanner()
		self._makeStartFail(
			BleakBluetoothNotAvailableError(
				"Bluetooth radio is not powered on",
				BleakBluetoothNotAvailableReason.POWERED_OFF,
			),
		)
		with self.assertRaises(BleakBluetoothNotAvailableError):
			scanner.start()
		self.assertFalse(scanner.isScanning)

	def test_startIsBounded(self):
		"""Talking to the Bluetooth stack is given a timeout."""
		scanner = self.Scanner()
		scanner.start()
		self.assertEqual(self.mockRunCoroutine.call_args.args[1], SCAN_CONTROL_TIMEOUT_SECONDS)

	def test_stopScanning(self):
		"""Test that stopping scan calls Bleak scanner and clears isScanning flag."""
		scanner = self.Scanner()
		scanner.start(duration=0)

		self.assertTrue(scanner.isScanning)

		scanner.stop()

		self.mockScannerInstance.stop.assert_called_once()
		self.assertFalse(scanner.isScanning)

	def test_sharedOwnersKeepScannerRunning(self):
		"""Closing settings does not interrupt automatic detection's watcher."""
		scanner = self.Scanner()
		detector, dialog = object(), object()
		scanner.acquire(detector)
		scanner.acquire(dialog)
		scanner.acquire(dialog)
		self.mockScannerInstance.start.assert_called_once()
		scanner.release(dialog)
		self.assertTrue(scanner.isScanning)
		self.mockScannerInstance.stop.assert_not_called()
		scanner.release(detector)
		self.mockScannerInstance.stop.assert_called_once()
		self.assertFalse(scanner.isScanning)

	def test_failedAcquireDoesNotKeepOwner(self):
		"""An unavailable radio can be retried by the same consumer later."""
		scanner = self.Scanner()
		owner = object()
		self._makeStartFail(TimeoutError("Bluetooth did not respond"))
		with self.assertRaises(TimeoutError):
			scanner.acquire(owner)
		self.assertNotIn(owner, scanner._owners)
		self.mockRunCoroutine.side_effect = None
		scanner.acquire(owner)
		self.assertTrue(scanner.isScanning)
		self.assertEqual(self.mockScannerInstance.start.call_count, 2)

	def test_stopReleasesAllOwners(self):
		"""Shutdown overrides every consumer, and repeated stop is harmless."""
		scanner = self.Scanner()
		scanner.acquire(object())
		scanner.acquire(object())
		scanner.stop()
		scanner.stop()
		self.assertFalse(scanner.isScanning)
		self.assertFalse(scanner._owners)
		self.mockScannerInstance.stop.assert_called_once()

	def test_terminateRejectsFurtherAcquisitions(self):
		"""Shutdown leaves a terminal scanner even when stopping the radio fails."""
		scanner = self.Scanner()
		scanner.acquire(object())
		self._makeStartFail(TimeoutError("stop timed out"))
		with self.assertRaises(TimeoutError):
			scanner.terminate()
		with self.assertRaisesRegex(RuntimeError, "terminated"):
			scanner.acquire(object())
		self.assertFalse(scanner._owners)
		self.mockScannerInstance.start.assert_called_once()

	def test_queuedAcquisitionCannotRestartAfterShutdown(self):
		"""A queued UI worker cannot reopen the old scanner after config reset."""
		scanner = self.Scanner()
		ready, resume = Event(), Event()
		errors = []

		def acquireLater():
			ready.set()
			if not resume.wait(timeout=2):
				return
			try:
				scanner.acquire(object())
			except RuntimeError as error:
				errors.append(error)

		worker = Thread(target=acquireLater, daemon=True)
		worker.start()
		try:
			self.assertTrue(ready.wait(timeout=2))
			scanner.terminate()
		finally:
			resume.set()
			worker.join(timeout=2)
		self.assertFalse(worker.is_alive())
		self.assertEqual(len(errors), 1)
		self.assertFalse(scanner.isScanning)
		self.mockScannerInstance.start.assert_not_called()

	def test_stopRemainsReusable(self):
		"""Ordinary stopping, unlike shutdown, still allows another scan."""
		scanner = self.Scanner()
		scanner.start()
		scanner.stop()
		scanner.start()
		self.assertTrue(scanner.isScanning)
		self.assertEqual(self.mockScannerInstance.start.call_count, 2)

	def test_deviceDiscoveredExtensionPoint(self):
		"""Test that deviceDiscovered extension point fires when device is advertised."""
		scanner = self.Scanner()

		handlerCalls = []

		def testHandler(device: BLEDevice, advertisementData: AdvertisementData, isNew: bool) -> None:
			handlerCalls.append(
				{
					"device": device,
					"advertisementData": advertisementData,
					"isNew": isNew,
				},
			)

		scanner.deviceDiscovered.register(testHandler)

		fakeDevice = MagicMock(spec=BLEDevice)
		fakeDevice.address = "AA:BB:CC:DD:EE:FF"
		fakeDevice.name = "TestDevice"
		fakeAdvData = MagicMock(spec=AdvertisementData)

		scanner._onDeviceAdvertised(fakeDevice, fakeAdvData)

		self.assertEqual(len(handlerCalls), 1)
		self.assertEqual(handlerCalls[0]["device"], fakeDevice)
		self.assertEqual(handlerCalls[0]["advertisementData"], fakeAdvData)
		self.assertTrue(handlerCalls[0]["isNew"])

		# Same device again - should not be new
		scanner._onDeviceAdvertised(fakeDevice, fakeAdvData)

		self.assertEqual(len(handlerCalls), 2)
		self.assertFalse(handlerCalls[1]["isNew"])

	def test_deviceTracking(self):
		"""Test that devices are tracked in internal dict and returned by results()."""
		scanner = self.Scanner()

		fakeDevice = MagicMock(spec=BLEDevice)
		fakeDevice.address = "AA:BB:CC:DD:EE:FF"
		fakeDevice.name = "TestDevice"
		fakeAdvData = MagicMock(spec=AdvertisementData)

		self.assertEqual(len(scanner.results()), 0)

		scanner._onDeviceAdvertised(fakeDevice, fakeAdvData)

		self.assertIn(fakeDevice.address, scanner._discoveredDevices)
		self.assertEqual(scanner._discoveredDevices[fakeDevice.address], fakeDevice)

		results = scanner.results()
		self.assertEqual(len(results), 1)
		self.assertEqual(results[0], fakeDevice)

	def test_resultsFiltering(self):
		"""Test that results() filter function works correctly."""
		scanner = self.Scanner()

		device1 = MagicMock(spec=BLEDevice)
		device1.address = "AA:BB:CC:DD:EE:01"
		device1.name = "TestDevice1"

		device2 = MagicMock(spec=BLEDevice)
		device2.address = "AA:BB:CC:DD:EE:02"
		device2.name = "OtherDevice"

		device3 = MagicMock(spec=BLEDevice)
		device3.address = "AA:BB:CC:DD:EE:03"
		device3.name = "TestDevice2"

		fakeAdvData = MagicMock(spec=AdvertisementData)

		scanner._onDeviceAdvertised(device1, fakeAdvData)
		scanner._onDeviceAdvertised(device2, fakeAdvData)
		scanner._onDeviceAdvertised(device3, fakeAdvData)

		allResults = scanner.results()
		self.assertEqual(len(allResults), 3)

		filteredResults = scanner.results(filterFunc=lambda d: d.name.startswith("Test"))
		self.assertEqual(len(filteredResults), 2)
		self.assertIn(device1, filteredResults)
		self.assertIn(device3, filteredResults)
		self.assertNotIn(device2, filteredResults)


class TestQueueReader(unittest.TestCase):
	"""Exercise asynchronous APC delivery with the real weak-reference contract."""

	def setUp(self):
		self.queue: Queue[bytes] = Queue()
		self.stopEvent = Event()
		self.queued = Event()
		self.callbacks = []
		self.received = []

		class DeferredIoThread:
			def queueAsApc(innerSelf, callback, param=0):
				self.callbacks.append((weakref.ref(callback), param))
				if len(self.callbacks) == 2:
					self.queued.set()

		self.reader = Thread(
			target=queueReader,
			args=(self.queue, self.received.append, self.stopEvent, DeferredIoThread()),
			daemon=True,
		)
		self.reader.start()

	def tearDown(self):
		self.stopEvent.set()
		self.reader.join(timeout=2)
		self.assertFalse(self.reader.is_alive())

	def _queuePackets(self):
		self.queue.put(b"first packet")
		self.queue.put(b"second packet")
		self.assertTrue(self.queued.wait(timeout=2))

	def test_delayedCallbacksKeepEachPacket(self):
		"""Back-to-back packets survive until IoThread dispatches their weak callbacks."""
		self._queuePackets()
		gc.collect()
		for callbackRef, param in self.callbacks:
			callback = callbackRef()
			self.assertIsNotNone(callback)
			callback(param)
		self.assertEqual(self.received, [b"first packet", b"second packet"])

	def test_pendingCallbacksDoNotRunAfterClose(self):
		"""A callback already retrieved by IoThread must still obey close()."""
		self._queuePackets()
		callbackRef, param = self.callbacks[0]
		callback = callbackRef()
		self.assertIsNotNone(callback)
		self.stopEvent.set()
		callback(param)
		self.assertEqual(self.received, [])


class TestBle(unittest.TestCase):
	"""Tests for hwIo.ble.Ble"""

	def setUp(self):
		"""Set up patches for Ble testing."""

		self.bleakClientPatcher = patch("hwIo.ble._io.bleak.BleakClient")
		self.mockBleakClientClass = self.bleakClientPatcher.start()

		# Use regular MagicMock for client methods (not AsyncMock):
		# the Ble class wraps every async call in runCoroutineSync(),
		# which is itself mocked, so the coroutines returned by
		# _initAndConnect / disconnect / write_gatt_char are immediately
		# closed by fakeRunCoroutineSync without ever being awaited. The
		# inner self._client.connect() etc. are therefore never called,
		# so they don't need to be awaitable.
		self.mockClient = MagicMock()
		self.mockClient.is_connected = True
		self.mockBleakClientClass.return_value = self.mockClient

		self.mockService = MagicMock()
		self.mockCharacteristic = MagicMock()
		self.mockCharacteristic.max_write_without_response_size = 20
		self.mockService.get_characteristic.return_value = self.mockCharacteristic

		self.mockServices = MagicMock()
		self.mockServices.get_service.return_value = self.mockService
		mockServicesDict = MagicMock()
		# Configure the MagicMock's __len__ via return_value because Python
		# looks up dunder methods on the type, not the instance.
		mockServicesDict.__len__.return_value = 1
		mockServicesDict.values.return_value = [self.mockService]
		self.mockServices.services = mockServicesDict
		self.mockClient.services = self.mockServices

		# Ble uses runCoroutineSync() which blocks until the coroutine
		# completes and returns the result directly. Close the passed
		# coroutine immediately so Python does not warn about "coroutine
		# was never awaited" — the mock will never actually run it.
		def fakeRunCoroutineSync(coro: object, timeout: float | None = None) -> None:
			if hasattr(coro, "close"):
				coro.close()

		self.runCoroutineSyncPatcher = patch(
			"hwIo.ble._io.runCoroutineSync",
			side_effect=fakeRunCoroutineSync,
		)
		self.mockRunCoroutineSync = self.runCoroutineSyncPatcher.start()

		self.Ble = Ble
		# Track Ble instances so tearDown can close them while patches
		# are still active (avoiding __del__ errors at GC time when the
		# real runCoroutineSync would be called against a dead event
		# loop).
		self._bleInstances: list[Ble] = []

	def tearDown(self):
		"""Clean up Ble instances and patches."""

		# Mark the mock client disconnected so the close() called from
		# Ble.__del__ at GC time becomes a no-op (it would otherwise hit
		# the real, unmocked runCoroutineSync and raise).
		self.mockClient.is_connected = False
		for ble in self._bleInstances:
			try:
				ble.close()
			except Exception:  # noqa: BLE001, S110
				pass
		self.runCoroutineSyncPatcher.stop()
		self.bleakClientPatcher.stop()

	def _makeBle(self, **kwargs) -> "object":
		"""Construct a Ble instance and track it for cleanup."""

		ble = self.Ble(**kwargs)
		self._bleInstances.append(ble)
		return ble

	def test_connectionSuccess(self):
		"""Test that Ble connects successfully and starts notifications."""
		mockDevice = MagicMock(spec=BLEDevice)
		mockDevice.address = "AA:BB:CC:DD:EE:FF"
		mockDevice.name = "TestDevice"

		mockIoThread = MagicMock()

		receivedData = []

		def onReceive(data: bytes) -> None:
			receivedData.append(data)

		ble = self._makeBle(
			device=mockDevice,
			writeServiceUuid="service-uuid",
			writeCharacteristicUuid="write-char-uuid",
			readServiceUuid="service-uuid",
			readCharacteristicUuid="read-char-uuid",
			onReceive=onReceive,
			ioThread=mockIoThread,
		)

		self.mockBleakClientClass.assert_called_once()
		callArgs = self.mockBleakClientClass.call_args
		self.assertEqual(callArgs[0][0], mockDevice)

		self.mockRunCoroutineSync.assert_called()
		self.assertTrue(ble.isConnected())

	def _bleKwargs(self, **overrides) -> dict:
		"""Build the constructor arguments for a Ble instance."""
		mockDevice = MagicMock(spec=BLEDevice)
		mockDevice.address = "AA:BB:CC:DD:EE:FF"
		mockDevice.name = "TestDevice"
		kwargs = {
			"device": mockDevice,
			"writeServiceUuid": "service-uuid",
			"writeCharacteristicUuid": "write-char-uuid",
			"readServiceUuid": "service-uuid",
			"readCharacteristicUuid": "read-char-uuid",
			"onReceive": lambda data: None,
			"ioThread": MagicMock(),
		}
		kwargs.update(overrides)
		return kwargs

	def test_connectIsBounded(self):
		"""The connection attempt is given a timeout, so a missing device cannot block forever."""
		self._makeBle(**self._bleKwargs())
		timeouts = [call.args[1] for call in self.mockRunCoroutineSync.call_args_list if len(call.args) > 1]
		self.assertIn(LINK_TIMEOUT_SECONDS, timeouts)

	def _makeConnectTimeOut(self) -> None:
		"""Make the next connection attempt time out.

		The coroutine is still closed, as the setUp fake does, so that Python does not
		warn that it was never awaited.
		"""

		failed: list[bool] = []

		def timeOut(coro: object, timeout: float | None = None) -> None:
			if hasattr(coro, "close"):
				coro.close()
			# Only the connection attempt fails; the clean-up that follows must still work.
			if not failed:
				failed.append(True)
				raise TimeoutError("timed out")

		self.mockRunCoroutineSync.side_effect = timeOut

	def test_connectTimeoutPropagates(self):
		"""A connection attempt that times out fails the constructor."""
		self._makeConnectTimeOut()
		with self.assertRaises(TimeoutError):
			self.Ble(**self._bleKwargs())

	def test_connectTimeoutStopsReaderThread(self):
		"""A failed constructor leaves no reader thread behind, as nothing owns the instance."""
		self._makeConnectTimeOut()
		with patch("hwIo.ble._io.Thread") as mockThread, self.assertRaises(TimeoutError):
			self.Ble(**self._bleKwargs())
		stopEvent = mockThread.call_args.kwargs["args"][2]
		self.assertTrue(stopEvent.is_set())

	def test_writeData(self):
		"""Test writing data to BLE characteristic."""
		mockDevice = MagicMock(spec=BLEDevice)
		mockDevice.address = "AA:BB:CC:DD:EE:FF"
		mockDevice.name = "TestDevice"
		mockIoThread = MagicMock()

		ble = self._makeBle(
			device=mockDevice,
			writeServiceUuid="service-uuid",
			writeCharacteristicUuid="write-char-uuid",
			readServiceUuid="service-uuid",
			readCharacteristicUuid="read-char-uuid",
			onReceive=lambda data: None,
			ioThread=mockIoThread,
		)

		testData = b"test data"
		ble.write(testData)

		self.mockServices.get_service.assert_called_with("service-uuid")
		self.mockService.get_characteristic.assert_called_with("write-char-uuid")

		self.assertGreater(self.mockRunCoroutineSync.call_count, 1)

	def test_writeDataChunking(self):
		"""Test that large data is split into MTU-sized chunks."""
		mockDevice = MagicMock(spec=BLEDevice)
		mockDevice.address = "AA:BB:CC:DD:EE:FF"
		mockDevice.name = "TestDevice"
		mockIoThread = MagicMock()

		self.mockCharacteristic.max_write_without_response_size = 10

		ble = self._makeBle(
			device=mockDevice,
			writeServiceUuid="service-uuid",
			writeCharacteristicUuid="write-char-uuid",
			readServiceUuid="service-uuid",
			readCharacteristicUuid="read-char-uuid",
			onReceive=lambda data: None,
			ioThread=mockIoThread,
		)

		initialCallCount = self.mockRunCoroutineSync.call_count

		testData = b"A" * 25
		ble.write(testData)

		writeCalls = self.mockRunCoroutineSync.call_count - initialCallCount
		self.assertEqual(writeCalls, 3)

	def test_writeTimeoutPropagates(self):
		"""A failed GATT write cannot hold NVDA's shared I/O thread indefinitely."""
		ble = self._makeBle(**self._bleKwargs())

		def failWrite(coro, timeout=None):
			if hasattr(coro, "close"):
				coro.close()
			self.assertEqual(timeout, LINK_TIMEOUT_SECONDS)
			raise TimeoutError("write timed out")

		self.mockRunCoroutineSync.side_effect = failWrite
		with self.assertRaises(TimeoutError):
			ble.write(b"display data")

	def test_receiveNotification(self):
		"""Test receiving data via BLE notification."""
		mockDevice = MagicMock(spec=BLEDevice)
		mockDevice.address = "AA:BB:CC:DD:EE:FF"
		mockDevice.name = "TestDevice"
		mockIoThread = MagicMock()
		mockIoThread.queueAsApc.side_effect = lambda callback, param: callback(param)

		receivedData: list[bytes] = []
		received = Event()

		def onReceive(data: bytes) -> None:
			receivedData.append(data)
			received.set()

		ble = self._makeBle(
			device=mockDevice,
			writeServiceUuid="service-uuid",
			writeCharacteristicUuid="write-char-uuid",
			readServiceUuid="service-uuid",
			readCharacteristicUuid="read-char-uuid",
			onReceive=onReceive,
			ioThread=mockIoThread,
		)

		self.mockRunCoroutineSync.assert_called()

		testData = bytearray(b"notification data")
		mockChar = MagicMock()
		ble._notifyReceive(mockChar, testData)

		self.assertTrue(received.wait(timeout=2))
		self.assertEqual(receivedData, [b"notification data"])
		self.assertIsInstance(receivedData[0], bytes)

	def test_closeCleanup(self):
		"""Test that close() properly disconnects and cleans up resources."""
		mockDevice = MagicMock(spec=BLEDevice)
		mockDevice.address = "AA:BB:CC:DD:EE:FF"
		mockDevice.name = "TestDevice"
		mockIoThread = MagicMock()

		ble = self._makeBle(
			device=mockDevice,
			writeServiceUuid="service-uuid",
			writeCharacteristicUuid="write-char-uuid",
			readServiceUuid="service-uuid",
			readCharacteristicUuid="read-char-uuid",
			onReceive=lambda data: None,
			ioThread=mockIoThread,
		)

		ble.close()

		self.assertGreater(self.mockRunCoroutineSync.call_count, 1)
		self.assertIsNone(ble._onReceive)
		callCount = self.mockRunCoroutineSync.call_count
		ble.close()
		self.assertEqual(self.mockRunCoroutineSync.call_count, callCount)

	def test_disconnectFailureStillStopsReader(self):
		"""A Bluetooth failure cannot leave a callback thread running after close."""
		ble = self._makeBle(**self._bleKwargs())

		def failDisconnect(coro, timeout=None):
			if hasattr(coro, "close"):
				coro.close()
			raise TimeoutError("disconnect timed out")

		self.mockRunCoroutineSync.side_effect = failDisconnect
		with self.assertRaises(TimeoutError):
			ble.close()
		self.assertFalse(ble._readerThread.is_alive())
		self.assertIsNone(ble._onReceive)
		ble.close()

	def test_notificationAfterCloseIsIgnored(self):
		ble = self._makeBle(**self._bleKwargs())
		ble.close()
		ble._notifyReceive(MagicMock(), bytearray(b"late packet"))
		self.assertTrue(ble._queuedData.empty())


class TestBleModuleLifecycle(unittest.TestCase):
	def test_shutdownTerminatesAnIdleScanner(self):
		"""No active scan is required for an old worker to hold a scanner reference."""
		with patch("hwIo.ble.scanner") as scanner:
			scanner.isScanning = False
			terminateBle()
			scanner.terminate.assert_called_once()


class TestIsAvailable(unittest.TestCase):
	"""Tests for hwIo.ble.isAvailable"""

	def _bluetooth(self, adapter: object):
		"""Make Windows report the given Bluetooth adapter, and run the query here."""

		async def getDefault():
			return adapter

		adapterClass = MagicMock()
		adapterClass.get_default_async = getDefault
		return (
			patch("_asyncioEventLoop.isRunning", return_value=True),
			patch("hwIo.ble.BluetoothAdapter", adapterClass),
			patch("hwIo.ble.runCoroutineSync", new=_runCoroutineHere),
		)

	def _adapter(self, radioState: object, centralRole: bool = True) -> MagicMock:
		"""Build an adapter reporting the given radio state and central role support."""
		radio = MagicMock()
		radio.state = radioState

		async def getRadio():
			return radio

		adapter = MagicMock()
		adapter.is_central_role_supported = centralRole
		adapter.get_radio_async = getRadio
		return adapter

	def _availableWith(self, adapter: object) -> bool:
		loopPatch, adapterPatch, runPatch = self._bluetooth(adapter)
		with loopPatch, adapterPatch, runPatch:
			return isAvailable()

	def test_noEventLoop(self):
		"""Without an event loop to ask on, availability is assumed.

		Hiding a driver on a machine that may well support BLE is worse than
		offering one that cannot connect.
		"""
		with patch("_asyncioEventLoop.isRunning", return_value=False):
			self.assertTrue(isAvailable())

	def test_noAdapter(self):
		"""A machine without a Bluetooth adapter cannot reach BLE devices."""
		self.assertFalse(self._availableWith(None))

	def test_radioOff(self):
		"""A switched off radio cannot reach BLE devices."""
		self.assertFalse(self._availableWith(self._adapter(RadioState.OFF)))

	def test_radioOn(self):
		"""A powered adapter able to act as a central can reach BLE devices."""
		self.assertTrue(self._availableWith(self._adapter(RadioState.ON)))

	def test_noCentralRole(self):
		"""An adapter that cannot act as a central is of no use for BLE."""
		self.assertFalse(self._availableWith(self._adapter(RadioState.ON, centralRole=False)))

	def test_failureToAsk(self):
		"""A question that cannot be answered does not hide the driver."""

		def fail(coro, timeout=None):
			coro.close()
			raise TimeoutError("timed out")

		with (
			patch("_asyncioEventLoop.isRunning", return_value=True),
			patch("hwIo.ble.runCoroutineSync", side_effect=fail),
		):
			self.assertTrue(isAvailable())


class TestGetDiscoveredDevice(unittest.TestCase):
	"""Tests for hwIo.ble.getDiscoveredDevice

	Unlike findDeviceByAddress this must be callable on the main thread,
	which is where a braille display chosen in the settings dialog is connected.
	These tests therefore deliberately do not patch out the main-thread check.
	"""

	def _fakeDevice(self, address: str) -> MagicMock:
		device = MagicMock(spec=BLEDevice)
		device.address = address
		return device

	def test_deviceInResults(self):
		"""The device with the requested address is returned."""
		wanted = self._fakeDevice("AA:BB:CC:DD:EE:FF")
		other = self._fakeDevice("11:22:33:44:55:66")
		with patch("hwIo.ble.scanner") as mockScanner:
			mockScanner.results.return_value = [other, wanted]
			self.assertIs(getDiscoveredDevice("AA:BB:CC:DD:EE:FF"), wanted)
			mockScanner.start.assert_not_called()

	def test_deviceNotInResults(self):
		"""None is returned without starting a scan."""
		with patch("hwIo.ble.scanner") as mockScanner:
			mockScanner.results.return_value = [self._fakeDevice("11:22:33:44:55:66")]
			self.assertIsNone(getDiscoveredDevice("AA:BB:CC:DD:EE:FF"))
			mockScanner.start.assert_not_called()

	def test_noScanner(self):
		"""None is returned when BLE was never initialized."""
		with patch("hwIo.ble.scanner", None):
			self.assertIsNone(getDiscoveredDevice("AA:BB:CC:DD:EE:FF"))


class TestFindDeviceByAddress(unittest.TestCase):
	"""Tests for hwIo.ble.findDeviceByAddress"""

	def setUp(self):
		"""Set up patches for findDeviceByAddress testing."""

		self.scannerPatcher = patch("hwIo.ble.scanner")
		self.mockScanner = self.scannerPatcher.start()

		# findDeviceByAddress is decorated with @requiresBackgroundThread; patch out the
		# main-thread check so tests can call it directly without spawning threads.
		self.mainThreadPatcher = patch("hwIo.base.threading.main_thread", return_value=MagicMock())
		self.mainThreadPatcher.start()

		self.findDeviceByAddress = findDeviceByAddress

	def tearDown(self):
		"""Clean up patches."""

		self.mainThreadPatcher.stop()
		self.scannerPatcher.stop()

	def test_deviceAlreadyInResults(self):
		"""Test finding device that's already in scanner results."""
		fakeDevice = MagicMock(spec=BLEDevice)
		fakeDevice.address = "AA:BB:CC:DD:EE:FF"
		fakeDevice.name = "TestDevice"

		self.mockScanner.results.return_value = [fakeDevice]
		self.mockScanner.isScanning = False

		result = self.findDeviceByAddress("AA:BB:CC:DD:EE:FF")

		self.assertEqual(result, fakeDevice)
		self.mockScanner.start.assert_not_called()
		self.mockScanner.acquire.assert_not_called()

	def test_deviceNotFound(self):
		"""Test that None is returned when device is not found after timeout."""
		self.mockScanner.results.return_value = []
		self.mockScanner.isScanning = False

		result = self.findDeviceByAddress("AA:BB:CC:DD:EE:FF", timeout=0.1)

		self.assertIsNone(result)
		self.mockScanner.acquire.assert_called_once()
		self.mockScanner.release.assert_called_once_with(self.mockScanner.acquire.call_args.args[0])

	def test_deviceFoundDuringScan(self):
		"""Test finding device that appears during scanning."""
		fakeDevice = MagicMock(spec=BLEDevice)
		fakeDevice.address = "AA:BB:CC:DD:EE:FF"
		fakeDevice.name = "TestDevice"

		callCount = 0

		def mockResults() -> list[BLEDevice]:
			nonlocal callCount
			callCount += 1
			if callCount <= 1:
				return []
			else:
				return [fakeDevice]

		self.mockScanner.results.side_effect = mockResults
		self.mockScanner.isScanning = False

		result = self.findDeviceByAddress("AA:BB:CC:DD:EE:FF", timeout=0.5, pollInterval=0.05)

		self.assertEqual(result, fakeDevice)
		self.mockScanner.acquire.assert_called_once()
		self.mockScanner.release.assert_called_once_with(self.mockScanner.acquire.call_args.args[0])
