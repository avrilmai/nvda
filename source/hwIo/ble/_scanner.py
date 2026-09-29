# A part of NonVisual Desktop Access (NVDA)
# Copyright (C) 2025-2026 NV Access Limited, Dot Incorporated, Bram Duvigneau
# This file may be used under the terms of the GNU General Public License, version 2 or later, as modified by the NVDA license.
# For full terms and any additional permissions, see the NVDA license file: https://github.com/nvaccess/nvda/blob/master/copying.txt

import time  # noqa: I001
from threading import Event, Lock
from collections.abc import Callable

from _asyncioEventLoop.utils import runCoroutineSync
import extensionPoints
from logHandler import log

import bleak
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

SCAN_CONTROL_TIMEOUT_SECONDS: int = 10
"""How long to wait for a scan to start or stop.

Both reach the Bluetooth stack, which can be slow to answer when the adapter is
busy, so the wait is bounded while staying generous enough not to give up on a
stack that is merely slow.
"""


class Scanner:
	"""Scan for BLE devices

	This is a small synchronous wrapper around Bleak's Scanner.
	It allows starting and stopping scans, retrieving results, and checking if scanning is active.
	"""

	_scanner: bleak.BleakScanner
	_discoveredDevices: dict[str, BLEDevice]
	_isScanning: Event

	def __init__(self):
		self._discoveredDevices = {}
		self._scanner = bleak.BleakScanner(self._onDeviceAdvertised)
		self._isScanning = Event()
		self._controlLock = Lock()
		self._resultsLock = Lock()
		self._owners: set[object] = set()
		self._legacyOwner = object()
		self._terminated = False
		#: Action called when a BLE device is discovered or re-advertises.
		#: Handlers receive: device (BLEDevice), advertisementData (AdvertisementData), isNew (bool)
		self.deviceDiscovered = extensionPoints.Action()

	def _onDeviceAdvertised(self, device: BLEDevice, adv: AdvertisementData) -> None:
		with self._resultsLock:
			isNew = device.address not in self._discoveredDevices
			# Unnamed devices can still be found by address.
			self._discoveredDevices[device.address] = device

		# Notify extension point handlers
		self.deviceDiscovered.notify(device=device, advertisementData=adv, isNew=isNew)

		if isNew:
			log.debug(f"Discovered BLE device: {device.name or device.address}")

	def acquire(self, owner: object) -> None:
		"""Keep scanning until this owner releases its interest.

		Calls for each owner must be ordered by the caller. This may block while
		starting the Bluetooth watcher, so callers must use a worker thread, never
		the asyncio thread or a discovery callback. Repeated acquisition is idempotent.
		"""
		with self._controlLock:
			if self._terminated:
				raise RuntimeError("BLE scanner has been terminated")
			if owner in self._owners:
				return
			if not self.isScanning:
				with self._resultsLock:
					self._discoveredDevices.clear()
				runCoroutineSync(self._scanner.start(), SCAN_CONTROL_TIMEOUT_SECONDS)
				self._isScanning.set()
			self._owners.add(owner)

	def release(self, owner: object) -> None:
		"""Release one owner's interest, stopping only when nobody needs the scan.

		Like acquire(), this must be called on a worker thread and may block.
		"""
		with self._controlLock:
			self._owners.discard(owner)
			if self._terminated:
				return
			if not self._owners and self.isScanning:
				runCoroutineSync(self._scanner.stop(), SCAN_CONTROL_TIMEOUT_SECONDS)
				self._isScanning.clear()

	def start(self, duration: float = 0):
		"""Start scanning for BLE devices.

		:param duration: If 0 (default), scan continues in background until stop() is called.
			If > 0, scan for specified duration in seconds then stop automatically.
		:raises bleak.exc.BleakError: If scanning could not be started, for example because
			the machine has no Bluetooth adapter or its radio is switched off.
		"""
		log.debug("Scanning for devices")
		self.acquire(self._legacyOwner)
		if duration > 0:
			time.sleep(duration)
			self.release(self._legacyOwner)

	def stop(self):
		"""Stop scanning unconditionally, releasing all owners (for shutdown).

		:raises bleak.exc.BleakError: If the scan could not be stopped.
		"""
		with self._controlLock:
			if self.isScanning:
				runCoroutineSync(self._scanner.stop(), SCAN_CONTROL_TIMEOUT_SECONDS)
				self._isScanning.clear()
			self._owners.clear()

	def terminate(self) -> None:
		"""Permanently close this scanner, rejecting queued or future acquisitions.

		An old settings or detection worker may finish after a configuration reset.
		It must not restart a watcher belonging to the terminated hwIo instance.
		"""
		with self._controlLock:
			if self._terminated:
				return
			self._terminated = True
			self._owners.clear()
			if self.isScanning:
				runCoroutineSync(self._scanner.stop(), SCAN_CONTROL_TIMEOUT_SECONDS)
				self._isScanning.clear()

	def results(self, filterFunc: Callable[[BLEDevice], bool] | None = None) -> list[BLEDevice]:
		"""Get the discovered BLE devices.

		:param filterFunc: Optional filter function to select specific devices.
		:return: List of BLE devices found during the scan, optionally filtered.
		"""
		with self._resultsLock:
			results = list(self._discoveredDevices.values())
		if filterFunc:
			results = [device for device in results if filterFunc(device)]
		return results

	@property
	def isScanning(self) -> bool:
		"""Check if scanning is currently active"""
		return self._isScanning.is_set()
