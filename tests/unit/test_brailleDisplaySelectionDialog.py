# A part of NonVisual Desktop Access (NVDA)
# Copyright (C) 2026 NV Access Limited
# This file may be used under the terms of the GNU General Public License, version 2 or later, as modified by the NVDA license.
# For full terms and any additional permissions, see the NVDA license file: https://github.com/nvaccess/nvda/blob/master/copying.txt

"""Tests for stable port selection and background BLE scanning in the display dialog."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gui.settingsDialogs import BrailleDisplaySelectionDialog


class TestBrailleDisplaySelectionDialog(unittest.TestCase):
	_PORT = "ble:DotPad320@AA:BB:CC:DD:EE:FF"

	def _dialog(self):
		dialog = SimpleNamespace(
			displayNames=["dotPad"],
			displayList=MagicMock(),
			portsList=MagicMock(),
			autoDetectList=MagicMock(),
			possiblePorts=[],
			_unavailablePorts=set(),
			_BLE_REFRESH_INTERVAL=1000,
			_refreshBlePorts=MagicMock(),
			_connecting=False,
		)
		dialog.displayList.GetSelection.return_value = 0
		return dialog

	def test_missingConfiguredPortKeepsSelection(self):
		"""An offline device stays selected instead of silently choosing an unrelated port."""
		dialog = self._dialog()
		with (
			patch("config.conf", {"braille": {"dotPad": {"port": self._PORT}}}),
			patch("braille.display._getDisplayDriver") as mockDriver,
		):
			mockDriver.return_value.getPossiblePorts.return_value = {"COM5": "Serial COM5"}
			BrailleDisplaySelectionDialog.updateStateDependentControls(dialog)
		self.assertEqual([port for port, _description in dialog.possiblePorts], ["COM5", self._PORT])
		dialog.portsList.SetSelection.assert_called_once_with(1)
		self.assertIn(self._PORT, dialog.possiblePorts[1][1])
		self.assertEqual(dialog._unavailablePorts, {self._PORT})

	def test_missingConfiguredPortIsListedWhenNoOthersExist(self):
		dialog = self._dialog()
		with (
			patch("config.conf", {"braille": {"dotPad": {"port": self._PORT}}}),
			patch("braille.display._getDisplayDriver") as mockDriver,
		):
			mockDriver.return_value.getPossiblePorts.return_value = {}
			BrailleDisplaySelectionDialog.updateStateDependentControls(dialog)
		self.assertEqual([port for port, _description in dialog.possiblePorts], [self._PORT])
		dialog.portsList.SetSelection.assert_called_once_with(0)

	def test_rediscoveryUpdatesDescriptionWithoutMovingSelection(self):
		dialog = self._dialog()
		dialog.possiblePorts = [("COM5", "Serial COM5"), (self._PORT, "Unavailable")]
		dialog._unavailablePorts = {self._PORT}
		dialog.portsList.GetSelection.return_value = 1
		with (
			patch("braille.display._getDisplayDriver") as mockDriver,
			patch("gui.settingsDialogs.wx.CallLater"),
			patch("gui.settingsDialogs.ui.message") as mockMessage,
		):
			mockDriver.return_value._getBlePorts.return_value = [(self._PORT, "Bluetooth DotPad320")]
			BrailleDisplaySelectionDialog._refreshBlePorts(dialog)
		self.assertEqual(dialog.possiblePorts, [("COM5", "Serial COM5"), (self._PORT, "Bluetooth DotPad320")])
		dialog.portsList.SetSelection.assert_called_once_with(1)
		self.assertEqual(dialog._unavailablePorts, set())
		mockMessage.assert_called_once()

	def test_unchangedScanDoesNotInterruptSpeech(self):
		dialog = self._dialog()
		dialog.possiblePorts = [(self._PORT, "Bluetooth DotPad320")]
		with (
			patch("braille.display._getDisplayDriver") as mockDriver,
			patch("gui.settingsDialogs.wx.CallLater"),
			patch("gui.settingsDialogs.ui.message") as mockMessage,
		):
			mockDriver.return_value._getBlePorts.return_value = list(dialog.possiblePorts)
			BrailleDisplaySelectionDialog._refreshBlePorts(dialog)
		mockMessage.assert_not_called()
		dialog.portsList.SetItems.assert_not_called()

	def test_connectionPreventsReentrantConfirmationAndCancellation(self):
		"""Pumped events cannot start a second connection or close the dialog midway."""
		dialog = SimpleNamespace(_connecting=True)
		BrailleDisplaySelectionDialog.onOk(dialog, None)
		BrailleDisplaySelectionDialog.onCancel(dialog, None)

	def test_closeDuringScannerStartReleasesLeaseAfterStart(self):
		"""Closing does not wait for Bluetooth and cannot release before acquisition finishes."""
		started = Event()
		allowStartToFinish = Event()
		executor = ThreadPoolExecutor(max_workers=1)
		dialog = SimpleNamespace(
			_bleScanOwner=object(),
			_bleScanExecutor=executor,
			_updateBleScanLease=BrailleDisplaySelectionDialog._updateBleScanLease,
		)
		calls = []

		def acquire(owner):
			calls.append(("acquire", owner))
			started.set()
			if not allowStartToFinish.wait(5):
				raise TimeoutError("Test did not release the mocked Bluetooth start")

		with patch("hwIo.ble.scanner") as scanner:
			dialog._bleScanner = scanner
			scanner.acquire.side_effect = acquire
			scanner.release.side_effect = lambda owner: calls.append(("release", owner))
			try:
				BrailleDisplaySelectionDialog._startBleScanner(dialog)
				self.assertTrue(started.wait(2))
				BrailleDisplaySelectionDialog._stopBleScanner(dialog)
				self.assertIsNone(dialog._bleScanExecutor)
				scanner.release.assert_not_called()
			finally:
				allowStartToFinish.set()
				executor.shutdown(wait=True)
			self.assertEqual(calls, [("acquire", dialog._bleScanOwner), ("release", dialog._bleScanOwner)])
			BrailleDisplaySelectionDialog._stopBleScanner(dialog)
			scanner.release.assert_called_once_with(dialog._bleScanOwner)
