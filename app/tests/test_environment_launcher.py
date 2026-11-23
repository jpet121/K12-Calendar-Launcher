"""Profile switching uses actual localhost API with mocked Oracle calls only."""
import copy
import json
from pathlib import Path
import sys
import threading
import unittest

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))
sys.path.insert(0, str(PACKAGE / 'tests'))
import launcher
from environments import resolve_config, state_key
import test_launcher_workflow as workflow


class ProfileLauncherTests(unittest.TestCase):
    tearDown = workflow.LauncherHTTPWorkflowTests.tearDown
    request = workflow.LauncherHTTPWorkflowTests.request
    wait = workflow.LauncherHTTPWorkflowTests.wait
    action = workflow.LauncherHTTPWorkflowTests.action
    connect = workflow.LauncherHTTPWorkflowTests.connect

    def setUp(self):
        workflow.LauncherHTTPWorkflowTests.setUp(self)
        self.profiles = {'active_profile': 'alpha', 'profiles': {
            'alpha': {'name': 'Alpha Test', 'environment': 'https://alpha.example.invalid'},
            'beta': {'name': 'Beta Test', 'environment': 'https://beta.example.invalid'},
            'alpha-second': {'name': 'Alpha separate namespace', 'environment': 'https://alpha.example.invalid'},
        }, 'request_timeout_seconds': 30, 'poll_timeout_seconds': 300}
        self.app.config_source = copy.deepcopy(self.profiles)
        self.app.select_environment('alpha')

    def test_switch_clears_auth_plans_downloads_and_loads_only_selected_receipts(self):
        self.connect()
        old_dir = self.app.state_dir
        receipt = old_dir / 'retained.json'
        receipt.write_text('{}')
        self.app.attach_receipt({'receipt_path': str(receipt)}, 'apply_calendar_edit')
        old_ids = set(self.app.receipts)
        token = self.app.register_file(receipt)['download_id']
        preview = self.action('preview_create', example=True)['result']
        self.assertEqual(preview['preview']['environment'], 'https://alpha.example.invalid')
        self.assertTrue(self.app.plans)
        selected = self.action('select_environment', profile_id='beta')
        self.assertEqual(selected['environment'], 'https://beta.example.invalid')
        self.assertFalse(selected['signed_in'])
        self.assertIsNone(self.app.snapshot)
        self.assertIsNone(self.app.service)
        self.assertIsNone(self.app.event_service)
        self.assertEqual(self.app.plans, {})
        self.assertEqual(self.app.downloads, {})
        self.assertEqual(self.app.receipts, {})
        self.assertNotEqual(self.app.state_dir, old_dir)
        self.assertTrue(receipt.is_file())
        self.assertEqual(self.request('GET', '/api/download/' + token)[0], 404)
        self.action('apply_create', error=True, preview_id=preview['preview_id'])
        self.assertEqual(self.world.instances, [])
        self.action('select_environment', profile_id='alpha')
        self.assertEqual(self.app.state_dir, old_dir)
        self.assertEqual(set(self.app.receipts), old_ids)
        self.assertFalse(self.app.authorization)
        self.assertEqual(self.app.downloads, {})

    def test_switch_cannot_interrupt_oracle_job(self):
        entered, released = threading.Event(), threading.Event()
        self.gates.append(released)
        self.transport.pause_next_read = (entered, released)
        status, job, _ = self.request('POST', '/api/action', {
            'action': 'connect', 'username': 'offline-operator', 'password': 'offline-test-password'})
        self.assertEqual(status, 202)
        self.assertTrue(entered.wait(3))
        self.assertEqual(self.request('POST', '/api/action', {'action': 'select_environment', 'profile_id': 'beta'})[0], 409)
        self.assertEqual(self.app.config['profile_id'], 'alpha')
        released.set()
        self.wait(job['job_id'])

    def test_stale_browser_target_blocked_before_using_new_credentials(self):
        self.action('select_environment', profile_id='beta')
        status, value, _ = self.request('POST', '/api/action', {'action': 'connect',
            'username': 'offline-operator', 'password': 'offline-test-password',
            'expected_profile_id': 'alpha', 'expected_environment': 'https://alpha.example.invalid'})
        self.assertEqual(status, 400)
        self.assertIn('environment changed', value['error'])
        self.assertFalse(self.app.authorization)
        self.assertEqual(self.transport.requests, [])

    def test_other_target_reaches_factory_and_workbook_plan(self):
        observed = []
        factory = self.app.service_factory
        def capture(auth, config, folder, progress):
            observed.append((config['environment'], config['profile_id'], folder))
            return factory(auth, config, folder, progress)
        self.app.service_factory = capture
        self.action('select_environment', profile_id='beta')
        self.connect()
        preview = self.action('preview_create', example=True)['result']
        seen = []
        def run(plan, auth, output, progress):
            seen.append((plan, output))
            return {'status': 'applied_verified'}
        self.app.create_runner = run
        result = self.action('apply_create', preview_id=preview['preview_id'])['result']
        self.assertEqual(observed[0][:2], ('https://beta.example.invalid', 'beta'))
        self.assertEqual(preview['profile_name'], 'Beta Test')
        self.assertEqual(seen[0][0]['environment'], 'https://beta.example.invalid')
        self.assertTrue(seen[0][1].is_relative_to(self.app.state_dir))
        self.assertEqual(result['environment'], 'https://beta.example.invalid')

    def test_same_origin_separate_profile_and_changed_origin_separate_state(self):
        alpha_dir = self.app.state_dir
        self.action('select_environment', profile_id='alpha-second')
        self.assertNotEqual(self.app.state_dir, alpha_dir)
        self.app.config_source['profiles']['alpha']['environment'] = 'https://replacement.example.invalid'
        self.action('select_environment', profile_id='alpha')
        self.assertNotEqual(self.app.state_dir, alpha_dir)
        self.assertEqual(self.app.state_dir.name, state_key(self.app.config))
        self.assertFalse(self.app.authorization)

    def test_restart_loads_source_profiles_and_target_scoped_state(self):
        (self.root / 'config.json').write_text(json.dumps(self.profiles))
        restarted = launcher.Application(self.root)
        self.assertIsNone(restarted.config)
        self.assertIsNone(restarted.state_dir)
        self.assertEqual(restarted.public_state()['environment'], '')
        self.assertEqual(len(restarted.public_state()['profiles']), 3)
        restarted.select_environment('beta')
        self.assertEqual(restarted.config['profile_id'], 'beta')
        self.assertEqual(restarted.config_source, self.profiles)

    def test_invalid_profile_does_not_contact_oracle_or_change_target(self):
        state = self.action('select_environment', error=True, profile_id='unconfigured')
        self.assertEqual(state['profile_id'], 'alpha')
        self.assertEqual(self.transport.requests, [])

    def test_disconnect_invalidates_downloads_and_previews(self):
        self.connect()
        receipt = self.app.state_dir / 'result.json'
        receipt.write_text('{}')
        download = self.app.register_file(receipt)['download_id']
        self.action('preview_create', example=True)
        self.action('disconnect')
        self.assertEqual(self.app.plans, {})
        self.assertEqual(self.app.downloads, {})
        self.assertEqual(self.request('GET', '/api/download/' + download)[0], 404)


if __name__ == '__main__':
    unittest.main()
