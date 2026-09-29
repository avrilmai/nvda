# A part of NonVisual Desktop Access (NVDA)
# Copyright (C) 2023-2026 NV Access Limited, Babbage B.V., Leonard de Ruijter, Dot Incorporated, Bram Duvigneau
# This file may be used under the terms of the GNU General Public License, version 2 or later, as modified by the NVDA license.
# For full terms and any additional permissions, see the NVDA license file: https://github.com/nvaccess/nvda/blob/master/copying.txt

"""Unit tests for the bdDetect module."""

import unittest  # noqa: I001
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch
import bdDetect
from .extensionPointTestHelpers import chainTester
import braille
from braille.brailleHandler import BrailleHandler
from brailleDisplayDrivers import dotPad
from utils.blockUntilConditionMet import blockUntilConditionMet


class TestBdDetectExtensionPoints(unittest.TestCase):
	"""A test for the extension points on the bdDetect module."""

	def test_scanForDevices(self):
		kwargs = dict(usb=False, bluetooth=False, ble=False, limitToDevices=["noBraille"])  # noqa: C408
		with chainTester(
			self,
			bdDetect.scanForDevices,
			[("noBraille", bdDetect.DeviceMatch("", "", "", {}))],
			**kwargs,
		):
			braille.handler._enableDetection(**kwargs)
			# wait for the detector to be terminated.
			success, _endTimeOrNone = blockUntilConditionMet(
				getValue=lambda: braille.handler._detector,
				giveUpAfterSeconds=3.0,
				shouldStopEvaluator=lambda detector: detector is None,
			)
			self.assertTrue(success)


class TestDriverRegistration(unittest.TestCase):
	"""A test for driver device registration."""

	def tearDown(self):
		bdDetect._driverDevices.clear()

	def test_addUsbDevice(self):
		"""Test adding a USB device."""
		from brailleDisplayDrivers import albatross

		registrar = bdDetect.DriverRegistrar(albatross.BrailleDisplayDriver.name)

		def matchFunc(match: bdDetect.DeviceMatch) -> bool:
			return match.deviceInfo.get("busReportedDeviceDescription") == albatross.driver.BUS_DEVICE_DESC

		registrar.addUsbDevice(
			bdDetect.ProtocolType.SERIAL,
			albatross.driver.VID_AND_PID,
			matchFunc=matchFunc,
		)
		expected = bdDetect._UsbDeviceRegistryEntry(
			albatross.driver.VID_AND_PID,
			bdDetect.ProtocolType.SERIAL,
			matchFunc=matchFunc,
		)
		self.assertIn(expected, registrar._getDriverDict().get(bdDetect.CommunicationType.USB))

	def test_addUsbDevices(self):
		"""Test adding multiple USB devices."""
		from brailleDisplayDrivers import albatross

		registrar = bdDetect.DriverRegistrar(albatross.BrailleDisplayDriver.name)

		def matchFunc(match: bdDetect.DeviceMatch) -> bool:
			return match.deviceInfo.get("busReportedDeviceDescription") == albatross.driver.BUS_DEVICE_DESC

		fakeVidAndPid = "VID_0403&PID_6002"
		registrar.addUsbDevices(
			bdDetect.ProtocolType.SERIAL,
			{albatross.driver.VID_AND_PID, fakeVidAndPid},
			matchFunc=matchFunc,
		)
		expected = bdDetect._UsbDeviceRegistryEntry(
			albatross.driver.VID_AND_PID,
			bdDetect.ProtocolType.SERIAL,
			matchFunc=matchFunc,
		)
		self.assertIn(expected, registrar._getDriverDict().get(bdDetect.CommunicationType.USB))
		expected2 = bdDetect._UsbDeviceRegistryEntry(
			fakeVidAndPid,
			bdDetect.ProtocolType.SERIAL,
			matchFunc=matchFunc,
		)
		self.assertIn(expected2, registrar._getDriverDict().get(bdDetect.CommunicationType.USB))

	def test_addBluetoothDevices(self):
		"""Test adding a fake Bluetooth match func."""
		from brailleDisplayDrivers import albatross

		registrar = bdDetect.DriverRegistrar(albatross.BrailleDisplayDriver.name)

		def matchFunc(match: bdDetect.DeviceMatch) -> bool:
			return True

		registrar.addBluetoothDevices(matchFunc)
		self.assertEqual(registrar._getDriverDict().get(bdDetect.CommunicationType.BLUETOOTH), matchFunc)

	def test_addBleDevices(self):
		"""addBleDevices stores the match function under the BLE communication type."""
		registrar = bdDetect.DriverRegistrar(dotPad.BrailleDisplayDriver.name)

		def matchFunc(match: bdDetect.DeviceMatch) -> bool:
			return match.id.startswith("DotPad")

		registrar.addBleDevices(matchFunc)

		storedMatchFunc = registrar._getDriverDict().get(bdDetect.CommunicationType.BLE)
		self.assertEqual(storedMatchFunc, matchFunc)
		self.assertTrue(callable(storedMatchFunc))

	def test_bleDeviceMatching(self):
		"""The registered DotPad match function accepts DotPad devices and rejects others."""
		registrar = bdDetect.DriverRegistrar(dotPad.BrailleDisplayDriver.name)
		registrar.addBleDevices(dotPad.BrailleDisplayDriver._isBleDotPad)

		matchingDevice = bdDetect.DeviceMatch(
			type=bdDetect.ProtocolType.BLE,
			id="DotPad320",
			port="AA:BB:CC:DD:EE:FF",
			deviceInfo={"name": "DotPad320", "address": "AA:BB:CC:DD:EE:FF"},
		)

		nonMatchingDevice = bdDetect.DeviceMatch(
			type=bdDetect.ProtocolType.BLE,
			id="SomeOtherDevice",
			port="11:22:33:44:55:66",
			deviceInfo={"name": "SomeOtherDevice", "address": "11:22:33:44:55:66"},
		)

		matchFunc = registrar._getDriverDict().get(bdDetect.CommunicationType.BLE)
		self.assertTrue(matchFunc(matchingDevice))
		self.assertFalse(matchFunc(nonMatchingDevice))


class TestBleDeviceDiscovery(unittest.TestCase):
	"""Tests for the detector reacting to BLE devices as they advertise.

	A background scan starts the scanner and moves on without waiting, so a device
	that is not already known reaches the detector only through this handler.
	"""

	_ADDRESS = "AA:BB:CC:DD:EE:FF"

	def _detector(self) -> MagicMock:
		"""Build a detector stub whose _onBleDeviceDiscovered is the real implementation."""
		detector = MagicMock(spec=bdDetect._Detector)
		detector._detectUsb = True
		detector._detectBluetooth = True
		detector._detectBle = True
		detector._limitToDevices = None
		detector._bleDeviceNames = {}
		detector._onBleDeviceDiscovered = bdDetect._Detector._onBleDeviceDiscovered.__get__(
			detector,
			type(detector),
		)
		return detector

	def _device(self, name: str | None) -> MagicMock:
		"""Build a stand-in for a discovered BLE device."""
		device = MagicMock()
		device.name = name
		device.address = self._ADDRESS
		return device

	def test_matchingDeviceQueuesScan(self):
		"""A newly discovered matching device is queued as the preferred device."""
		detector = self._detector()
		match = bdDetect.DeviceMatch(
			bdDetect.ProtocolType.BLE,
			"DotPad320",
			self._ADDRESS,
			{"name": "DotPad320", "address": self._ADDRESS},
		)
		detector._getBleDeviceMatch = MagicMock(return_value=("dotPad", match))

		detector._onBleDeviceDiscovered(self._device("DotPad320"), MagicMock(), True)

		detector._queueBgScan.assert_called_once_with(
			usb=True,
			bluetooth=True,
			ble=True,
			limitToDevices=None,
			preferredDevice=("dotPad", match),
		)

	def test_nonMatchingDeviceIsIgnored(self):
		"""A device no driver claims does not trigger a scan."""
		detector = self._detector()
		detector._getBleDeviceMatch = MagicMock(return_value=None)

		detector._onBleDeviceDiscovered(self._device("SomeOtherDevice"), MagicMock(), True)

		detector._queueBgScan.assert_not_called()

	def test_readvertisementIsIgnored(self):
		"""Only the first sighting queues a scan, so repeat advertisements stay cheap."""
		detector = self._detector()
		detector._getBleDeviceMatch = MagicMock()
		detector._bleDeviceNames[self._ADDRESS] = "DotPad320"

		detector._onBleDeviceDiscovered(self._device("DotPad320"), MagicMock(), False)

		detector._getBleDeviceMatch.assert_not_called()
		detector._queueBgScan.assert_not_called()

	def test_nameArrivingInLaterAdvertisementIsMatched(self):
		"""A device first seen without a name can still be discovered when the name arrives."""
		detector = self._detector()
		device = self._device(None)
		match = bdDetect.DeviceMatch(
			bdDetect.ProtocolType.BLE,
			"DotPad320",
			self._ADDRESS,
			{"name": "DotPad320", "address": self._ADDRESS},
		)
		detector._getBleDeviceMatch = MagicMock(side_effect=[None, ("dotPad", match)])
		detector._onBleDeviceDiscovered(device, MagicMock(), True)
		detector._queueBgScan.assert_not_called()
		# Bleak can update the same device object, so the cached name must be a value snapshot.
		device.name = "DotPad320"
		detector._onBleDeviceDiscovered(device, MagicMock(), False)
		detector._queueBgScan.assert_called_once_with(
			usb=True,
			bluetooth=True,
			ble=True,
			limitToDevices=None,
			preferredDevice=("dotPad", match),
		)

	def test_bleDetectionDisabled(self):
		"""No scan is queued while BLE detection is off."""
		detector = self._detector()
		detector._detectBle = False
		detector._getBleDeviceMatch = MagicMock()

		detector._onBleDeviceDiscovered(self._device("DotPad320"), MagicMock(), True)

		detector._queueBgScan.assert_not_called()


class TestBleDriverMatching(unittest.TestCase):
	"""Tests for matching discovered BLE devices against the registered drivers."""

	def tearDown(self) -> None:
		# Registration writes into the global registry; keep tests isolated.
		bdDetect._driverDevices.clear()

	def _registerDriver(self, driver: str, prefix: str) -> None:
		"""Register a driver claiming any device whose name starts with the given prefix."""
		registrar = bdDetect.DriverRegistrar(driver)
		registrar.addBleDevices(lambda match, prefix=prefix: match.id.startswith(prefix))

	def _scannerWith(self, *names: str):
		"""Make the shared scanner report devices with the given names."""
		devices = []
		for i, name in enumerate(names):
			device = MagicMock()
			device.name = name
			device.address = f"AA:BB:CC:DD:EE:{i:02X}"
			devices.append(device)
		scanner = MagicMock()
		scanner.isScanning = True
		scanner.results.return_value = devices
		return patch("hwIo.ble.scanner", scanner)

	def test_devicesAreOfferedInDiscoveryOrder(self):
		"""The device discovered first is offered first, whichever driver claims it.

		Driver registration order is import order, so ordering by driver would let that
		decide which display is connected when several are in range.
		"""
		self._registerDriver("driverA", "Alpha")
		self._registerDriver("driverB", "Beta")
		with self._scannerWith("Beta1", "Alpha1"):
			result = list(bdDetect.getDriversForBleDevices())
		self.assertEqual([driver for driver, match in result], ["driverB", "driverA"])
		self.assertEqual([match.id for driver, match in result], ["Beta1", "Alpha1"])

	def test_limitToDevicesExcludesDrivers(self):
		"""A driver outside the limit never sees the devices."""
		self._registerDriver("driverA", "Alpha")
		self._registerDriver("driverB", "Beta")
		with self._scannerWith("Beta1", "Alpha1"):
			result = list(bdDetect.getDriversForBleDevices(limitToDevices=["driverA"]))
		self.assertEqual([(driver, match.id) for driver, match in result], [("driverA", "Alpha1")])

	def test_noBleDriversYieldsNothing(self):
		"""Without a driver that can match BLE devices the scan results are not inspected."""
		with self._scannerWith("Alpha1") as scanner:
			self.assertEqual(list(bdDetect.getDriversForBleDevices()), [])
		scanner.results.assert_not_called()

	def test_unavailableScannerYieldsNothing(self):
		"""A registered BLE driver is safe to enumerate before BLE initialization."""
		self._registerDriver("driverA", "Alpha")
		with patch("hwIo.ble.scanner", None):
			self.assertEqual(list(bdDetect.getDriversForBleDevices()), [])

	def test_emptyDriverLimitDoesNotEnableAllDrivers(self):
		"""Excluding every driver must not fall back to unfiltered BLE detection."""
		self._registerDriver("driverA", "Alpha")
		with self._scannerWith("Alpha1") as scanner:
			self.assertEqual(list(bdDetect.getDriversForBleDevices(limitToDevices=[])), [])
			detector = bdDetect._Detector.__new__(bdDetect._Detector)
			detector._limitToDevices = []
			device = scanner.results.return_value[0]
			self.assertIsNone(detector._getBleDeviceMatch(device))
		scanner.results.assert_not_called()


class TestBleDetectorLifecycle(unittest.TestCase):
	"""BLE discovery must not delay the UI or disable the other transports."""

	def setUp(self):
		self.detector = bdDetect._Detector.__new__(bdDetect._Detector)
		self.detector._executor = MagicMock()
		self.detector._executorLock = threading.RLock()
		self.detector._terminated = False
		self.detector._queuedFuture = None
		self.detector._stopEvent = threading.Event()
		self.detector._bleScanner = None
		self.detector._bleDiscoveryScanner = None
		self.usbMatch = bdDetect.DeviceMatch(bdDetect.ProtocolType.SERIAL, "USB display", "COM1", {})

	def test_scannerUnavailableStillConnectsUsb(self):
		"""USB detection still works when no BLE scanner was initialized."""
		with (
			patch("hwIo.ble.scanner", None),
			patch.object(bdDetect.scanForDevices, "iter", return_value=iter([("usbDriver", self.usbMatch)])),
			patch("bdDetect.braille.handler") as handler,
		):
			handler.setDisplayByName.return_value = True
			self.detector._bgScan(True, True, True, None, None)
			handler.setDisplayByName.assert_called_once_with("usbDriver", detected=self.usbMatch)

	def test_scannerStartFailureStillConnectsUsb(self):
		"""A disabled Bluetooth adapter must not prevent USB detection."""
		with (
			patch("hwIo.ble.scanner") as scanner,
			patch("bdDetect._hasBleDrivers", return_value=True),
			patch.object(bdDetect.scanForDevices, "iter", return_value=iter([("usbDriver", self.usbMatch)])),
			patch("bdDetect.braille.handler") as handler,
		):
			scanner.acquire.side_effect = bdDetect.BleakError("Bluetooth adapter unavailable")
			handler.setDisplayByName.return_value = True
			self.detector._bgScan(True, True, True, None, None)
			handler.setDisplayByName.assert_called_once_with("usbDriver", detected=self.usbMatch)

	def test_alreadyRunningScannerIsAcquired(self):
		"""Discovery keeps a scan lease even if another caller started scanning."""
		with (
			patch("hwIo.ble.scanner") as scanner,
			patch("bdDetect._hasBleDrivers", return_value=True),
			patch.object(bdDetect.scanForDevices, "iter", return_value=iter([])),
		):
			scanner.isScanning = True
			self.detector._bgScan(True, True, True, None, None)
			scanner.acquire.assert_called_once_with(self.detector)
			scanner.release.assert_not_called()

	def test_stopQueuesReleaseWithoutWaitingForBluetooth(self):
		"""Stopping from the UI queues the release instead of calling the Bluetooth stack."""
		with patch("hwIo.ble.scanner") as scanner:
			self.detector._stopBgScan()
			self.assertTrue(self.detector._stopEvent.is_set())
			self.detector._executor.submit.assert_called_once_with(self.detector._releaseBleScanner)
			scanner.release.assert_not_called()
			scanner.stop.assert_not_called()

	def test_stoppedDuringScannerStartDoesNotConnectPreferredDevice(self):
		"""An interrupted scan does not connect a display after its slow scanner start returns."""
		with (
			patch("hwIo.ble.scanner") as scanner,
			patch("bdDetect._hasBleDrivers", return_value=True),
			patch("bdDetect.braille.handler") as handler,
		):
			scanner.acquire.side_effect = lambda owner: self.detector._stopEvent.set()
			self.detector._bgScan(True, True, True, None, ("usbDriver", self.usbMatch))
			handler.setDisplayByName.assert_not_called()

	def test_terminateDoesNotWaitForScannerRelease(self):
		"""Termination returns while a slow release finishes on the detector worker."""
		releaseStarted = threading.Event()
		allowRelease = threading.Event()
		releaseFinished = threading.Event()
		self.detector._executor = executor = ThreadPoolExecutor(1)

		def release(owner):
			releaseStarted.set()
			allowRelease.wait(3)
			releaseFinished.set()

		with (
			patch("hwIo.ble.scanner") as scanner,
			patch("bdDetect.deviceInfoFetcher"),
		):
			scanner.release.side_effect = release
			self.detector._bleScanner = scanner
			try:
				self.detector.terminate()
				self.assertTrue(releaseStarted.wait(1), "The scanner release was not queued")
				self.assertFalse(releaseFinished.is_set(), "Termination waited for the scanner release")
			finally:
				allowRelease.set()
				executor.shutdown(wait=True)
			scanner.release.assert_called_once_with(self.detector)
			self.assertTrue(releaseFinished.is_set())

	def test_releaseUsesAcquiredScannerAfterSingletonChanges(self):
		"""A delayed release belongs to the old scanner after configuration reset."""
		oldScanner = self.detector._bleScanner = MagicMock()
		with patch("hwIo.ble.scanner") as newScanner:
			self.detector._releaseBleScanner()
			oldScanner.release.assert_called_once_with(self.detector)
			newScanner.release.assert_not_called()
			self.assertIsNone(self.detector._bleScanner)

	def test_emptyDriverLimitDoesNotConnectPreferredDevice(self):
		"""An empty driver list disables detection, including a previously preferred device."""
		with patch("bdDetect.braille.handler") as handler:
			self.detector._bgScan(True, True, True, [], ("usbDriver", self.usbMatch))
			handler.setDisplayByName.assert_not_called()

	def test_terminatedDetectorDoesNotRestartScan(self):
		"""A callback racing with termination cannot start another scan."""
		self.detector._terminated = True
		with patch("hwIo.ble.scanner") as scanner:
			self.detector._queueBgScan(ble=True, limitToDevices=["dotPad"])
			self.detector._bgScan(True, True, True, None, None)
			self.detector._executor.submit.assert_not_called()
			scanner.acquire.assert_not_called()


class TestBleProfileDetection(unittest.TestCase):
	def test_profileChangeRestartsDetectionWithEnabledDrivers(self):
		"""Enabling a BLE driver in a profile starts detection even if no radio scan was running."""
		handler = MagicMock(spec=BrailleHandler)
		handler._lastRequestedDisplayName = "auto"
		handler.display = MagicMock()
		handler.display.name = "noBraille"
		handler._detector = MagicMock()
		handler._table = MagicMock()
		handler._table.fileName = "test.ctb"
		with (
			patch(
				"braille.brailleHandler.config.conf",
				{"braille": {"display": "auto", "tetherTo": "auto", "translationTable": "test.ctb"}},
			),
			patch("bdDetect.getBrailleDisplayDriversEnabledForDetection", return_value=iter(["dotPad"])),
		):
			BrailleHandler.handlePostConfigProfileSwitch(handler)
			handler._detector.rescan.assert_called_once_with(limitToDevices=["dotPad"])
