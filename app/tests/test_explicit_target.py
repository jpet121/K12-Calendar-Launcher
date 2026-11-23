"""Explicit target selection and fallback prevention; all clients are mocked."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import launcher
from environments import available_profiles, resolve_config
from operations import CalendarClient, CalendarService, OperationError
from event_updates import EventUpdateService, EventUpdateError
from engine import create_adapter
from engine.test_creation_adapter import make_plan, AUTH
import test_launcher_workflow as workflow


class ExplicitTargetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = {'active_profile': 'sample-a', 'profiles': {
            'sample-a': {'name': 'SAMPLE-A', 'environment': launcher.SAMPLE_A},
            'other': {'name': 'Other', 'environment': 'https://other.example.invalid'}}}
        self.factory = Mock(side_effect=AssertionError('Client construction must wait for explicit selection.'))

    def app(self, source=None):
        return launcher.Application(self.root, self.source if source is None else source,
                                    service_factory=self.factory)

    def test_configured_active_profile_does_not_select_target_or_load_state(self):
        app = self.app()
        state = app.public_state()
        self.assertIsNone(app.config)
        self.assertIsNone(app.state_dir)
        for field in ('environment', 'profile_id', 'profile_name', 'environment_state_key'):
            self.assertEqual(state[field], '')
        self.assertEqual(len(state['profiles']), 2)
        self.assertEqual(state['receipts'], [])
        self.assertFalse(state['signed_in'])
        self.assertFalse((self.root / 'state').exists())
        self.factory.assert_not_called()

    def test_restart_ignores_saved_active_target_and_preserves_state_until_selected(self):
        app = self.app()
        selected = app.select_or_add_url('https://new.example.invalid/hcmUI?session=transient')
        folder = app.state_dir
        receipt = folder / 'calendar_receipt.json'
        receipt.write_text('{}')
        app.attach_receipt({'receipt_path': str(receipt)}, 'apply_calendar_edit')
        app.downloads['old-download'] = receipt
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        restarted = self.app()
        self.assertIsNone(restarted.config)
        self.assertIsNone(restarted.state_dir)
        self.assertEqual(restarted.receipts, {})
        self.assertEqual(restarted.downloads, {})
        self.assertEqual({str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)
        restarted.select_environment(selected['profile_id'])
        self.assertEqual(restarted.state_dir, folder)
        self.assertTrue(restarted.receipts)
        self.assertFalse(restarted.authorization)
        self.factory.assert_not_called()

    def test_every_unselected_operation_blocks_without_service_or_target_files(self):
        app = self.app()
        for action, data in (
            ('connect', {'username': 'test', 'password': 'offline-secret'}),
            ('read', {}), ('export', {}), ('preview_create', {'example': True}),
            ('apply_create', {}), ('preview_calendar_edit', {}), ('apply_calendar_edit', {}),
            ('preview_event_edit', {}), ('apply_event_edit', {}), ('apply_delete', {}),
            ('check_delete', {}), ('prepare_acceptance', {}), ('run_acceptance', {}),
            ('reconcile_calendar', {}), ('prepare_restore', {}), ('reconcile_event', {})):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'Paste or select'):
                app.execute(action, data)
        self.assertFalse(app.authorization)
        self.assertFalse((self.root / 'state').exists())
        self.factory.assert_not_called()

    def test_blank_invalid_or_missing_selection_never_uses_catalog_default(self):
        for value in (None, '', ' ', 'http://other.example.invalid', 'https://user:secret@other.example.invalid'):
            with self.subTest(value=value):
                app = self.app()
                with self.assertRaises(ValueError):
                    app.execute('connect', {'environment_url': value, 'username': 'test', 'password': 'offline-secret'})
                self.assertIsNone(app.config)
                self.assertFalse(app.authorization)
                self.assertEqual(app.public_state()['receipts'], [])
        self.factory.assert_not_called()
        self.assertFalse((self.root / 'state').exists())

    def test_empty_catalog_supports_paste_connection_without_configuration_edits(self):
        source = {'profiles': {}}
        app = self.app(source)
        service = Mock()
        service.read.return_value = {'calendars': [], 'events': [], 'links': []}
        app.service_factory = Mock(return_value=service)
        result = app.execute('connect', {'environment_url': 'HTTPS://new.example.invalid:443/welcome?q=discard',
                                        'username': 'test', 'password': 'offline-secret'})
        self.assertEqual(result['status'], 'connected')
        self.assertEqual(app.config['environment'], 'https://new.example.invalid')
        self.assertEqual(source, {'profiles': {}})
        self.assertFalse((self.root / 'config.json').exists())
        selected = app.service_factory.call_args.args[1]
        self.assertEqual(selected['integration'], {})
        self.assertEqual(selected['environment'], 'https://new.example.invalid')
        self.assertNotIn('discard', (self.root / 'state' / 'saved_environments.json').read_text())

    def test_resolver_requires_explicit_profile_and_no_default_origin(self):
        self.assertEqual(len(available_profiles(self.source)), 2)
        with self.assertRaisesRegex(ValueError, 'Paste or select'):
            resolve_config(self.source)
        for value in ({}, {'environment': None}, {'environment': ''}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_config(value)
        self.assertEqual(resolve_config(self.source, 'other')['environment'], 'https://other.example.invalid')

    def test_all_operation_adapters_reject_missing_origin_before_client(self):
        with patch('operations.urllib.request.build_opener') as opener:
            with self.assertRaises(OperationError):
                CalendarClient('Basic synthetic', {})
            with self.assertRaises(OperationError):
                CalendarService('Basic synthetic', {}, self.root)
            opener.assert_not_called()
        with patch('event_updates.engine.Client') as event_client:
            with self.assertRaises(EventUpdateError):
                EventUpdateService('Basic synthetic', {}, self.root)
            event_client.assert_not_called()
        with patch('engine.create_adapter.core.Client') as create_client:
            result = create_adapter.run_create(make_plan(), AUTH, self.root / 'create')
            self.assertEqual(result['status'], 'failed')
            self.assertIsNone(result['environment'])
            create_client.assert_not_called()


class UnselectedHTTPTests(unittest.TestCase):
    tearDown = workflow.LauncherHTTPWorkflowTests.tearDown
    request = workflow.LauncherHTTPWorkflowTests.request
    wait = workflow.LauncherHTTPWorkflowTests.wait
    action = workflow.LauncherHTTPWorkflowTests.action

    def setUp(self):
        workflow.LauncherHTTPWorkflowTests.setUp(self)
        # Reinitialize only the app state in the existing localhost server.
        self.app.__init__(self.root, self.config, service_factory=Mock(side_effect=AssertionError('Unexpected client')))

    def test_http_connect_without_target_fails_then_explicit_url_connects(self):
        state = self.action('connect', error=True, username='test', password='offline-secret')
        self.assertIn('Paste or select', state['error'])
        self.assertEqual(state['environment'], '')
        self.assertFalse(state['signed_in'])
        self.app.service_factory.assert_not_called()
        service = Mock()
        service.read.return_value = {'calendars': [], 'events': [], 'links': []}
        self.app.service_factory = Mock(return_value=service)
        state = self.action('connect', environment_url='https://explicit.example.invalid/hcmUI?state=discard',
                            username='test', password='offline-secret', expected_profile_id='', expected_environment='')
        self.assertEqual(state['environment'], 'https://explicit.example.invalid')
        self.assertTrue(state['signed_in'])
        self.assertEqual(self.app.service_factory.call_args.args[1]['environment'], state['environment'])
        self.assertEqual(self.transport.requests, [])


if __name__ == '__main__':
    unittest.main()
