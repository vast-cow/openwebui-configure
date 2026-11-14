"""Offline capability normalization, planning, and HTTP lifecycle regression tests."""
import contextlib
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import configure_openwebui_models as app


def deployment(name='public', **info):
    return {'model_name': name, 'model_info': info,
            'litellm_params': {'model': 'provider/private'}, 'id': 'deployment-id'}


def normalize(*rows):
    return app.normalize_litellm({'data': list(rows)})


def record(model_id='public', **overrides):
    result = {'id': model_id, 'name': model_id, 'base_model_id': None,
              'meta': {'hidden': True, 'description': 'keep'}, 'is_active': False,
              'params': {'temperature': 0.7, 'model_controls': {
                  'thinking': app.make_legacy_control('ultra'),
                  'custom': {'label': 'Custom'}}},
              'access_grants': [{'principal_type': 'group', 'principal_id': 'team',
                                 'permission': 'write'}]}
    result.update(overrides)
    return result


class CapabilityTests(unittest.TestCase):
    def test_chat_classification(self):
        cases = [({'mode': 'chat', 'supports_vision': True}, True),
                 ({'mode': 'responses'}, True),
                 ({'mode': 'responses', 'supported_endpoints': ['/v1/responses']}, True),
                 ({'mode': 'completion'}, None),
                 ({'mode': 'completion', 'supported_endpoints': ['/completions']}, None),
                 ({'mode': 'completion', 'supported_endpoints': ['/v1/chat/completions']}, True),
                 ({'mode': 'chat', 'supported_endpoints': ['/v1/embeddings']}, None),
                 ({'mode': 'embedding', 'supported_endpoints': ['/chat/completions']}, None),
                 ({'mode': 'chat', 'supported_endpoints': []}, None),
                 ({'mode': 'chat', 'supported_endpoints': 'bad'}, None),
                 ({'mode': 'future', 'supported_endpoints': ['/chat/completions']}, None),
                 ({}, None)]
        cases.extend(({'mode': mode}, False) for mode in
                     ['embedding', 'rerank', 'image_generation', 'audio_speech', 'moderation'])
        for info, expected in cases:
            with self.subTest(info=info):
                self.assertIs(app.litellm_chat_supported(info), expected)

    def test_effort_lists(self):
        for values in [None, 'low', ['low', 'low'], [1], [''], ['__proto__'],
                       ['constructor'], ['prototype'], ['low high']]:
            with self.subTest(values=values):
                self.assertIsNone(app.litellm_effort_options({'reasoning_effort_levels': values}))
        self.assertEqual(app.litellm_effort_options({'reasoning_effort_levels': []}), [])
        self.assertEqual(app.litellm_effort_options({'reasoning_effort_levels': ['low', 'xhigh']}),
                         ['low', 'xhigh'])
        self.assertIsNone(app.litellm_effort_options(
            {'supports_reasoning': True, 'supported_openai_params': ['reasoning_effort']}))

    def test_duplicates(self):
        first = deployment(mode='chat', reasoning_effort_levels=['low', 'high'])
        second = deployment(mode='chat', reasoning_effort_levels=['medium', 'high'])
        data, warnings = normalize(first, second)
        self.assertEqual(data['public']['reasoning_effort']['options'], ['high'])
        self.assertTrue(warnings)
        for extra in [deployment(mode='chat'), deployment(mode='chat', reasoning_effort_levels=None)]:
            data, _ = normalize(first, extra)
            self.assertIsNone(data['public']['reasoning_effort']['options'])
        data, _ = normalize(first, deployment(mode='embedding', reasoning_effort_levels=[]))
        self.assertIsNone(data['public']['chat_supported'])
        self.assertEqual(data['public']['reasoning_effort']['options'], [])
        data, _ = normalize(first, deployment(mode='chat', reasoning_effort_levels=['medium']))
        self.assertEqual(data['public']['reasoning_effort']['options'], [])

    def test_invalid_response(self):
        for data in [[], {}, {'data': {}}, {'data': [None]}, {'data': [{}]},
                     {'data': [{'model_name': 'public', 'model_info': 'bad'}]}]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                app.normalize_litellm(data)
        self.assertEqual(normalize()[0], {})

    def plan(self, rows, saved=None, **kwargs):
        metadata, _ = normalize(*rows)
        return app.make_plan_metadata(saved if saved is not None else [record()],
                                      [{'id': 'public'}], metadata,
                                      preserve_unknown_controls=True, upstream_only=True, **kwargs)

    def test_public_name_preset_and_unmatched(self):
        saved = [record(), record('preset', base_model_id='public'),
                 record('provider/private'), record('deployment-id'), record('unknown')]
        original = deepcopy(saved)
        plan = self.plan([deployment(mode='chat', reasoning_effort_levels=['low', 'high'])], saved)
        self.assertEqual(set(plan['changes_by_id']), {'public', 'preset'})
        self.assertEqual(set(plan['skipped_ids']), {'provider/private', 'deployment-id', 'unknown'})
        for imported in plan['imports']:
            controls = imported['params']['model_controls']
            self.assertNotIn('thinking', controls)
            self.assertEqual(list(controls['reasoning_effort']['options']), ['low', 'high'])
            self.assertIsNone(controls['reasoning_effort']['default'])
            self.assertEqual(controls['custom'], {'label': 'Custom'})
            self.assertEqual(imported['params']['temperature'], 0.7)
            self.assertEqual(imported['access_grants'], original[0]['access_grants'])
            self.assertTrue(imported['meta']['hidden'])
            self.assertFalse(imported['is_active'])
        self.assertEqual(saved, original)

    def assert_unmatched_preset_collision(self, info):
        for hidden in [False, True]:
            with self.subTest(hidden=hidden):
                preset = record('alias-a', base_model_id='different-upstream',
                                meta={'hidden': hidden})
                original = deepcopy(preset)
                metadata, _ = normalize(deployment('alias-a', **info))
                plan = app.make_plan_metadata(
                    [preset], [{'id': 'different-upstream'}], metadata,
                    unhide_chat=True, preserve_unknown_controls=True, upstream_only=True)
                self.assertIn('alias-a', plan['skipped_ids'])
                self.assertEqual(plan['imports'], [])
                self.assertEqual(plan['changes_by_id'], {})
                self.assertEqual(plan['public_ids'], [])
                self.assertEqual(plan['controlled_ids'], [])
                self.assertEqual(plan['verify_hidden'], {})
                self.assertEqual(plan['verify_controls'], {})
                self.assertEqual(preset, original)

    def test_litellm_preset_id_collision_with_nonchat(self):
        self.assert_unmatched_preset_collision({'mode': 'embedding'})

    def test_litellm_preset_id_collision_with_chat_efforts(self):
        self.assert_unmatched_preset_collision(
            {'mode': 'chat', 'reasoning_effort_levels': ['low', 'high']})

    def test_json_metadata_keeps_preset_id_fallback(self):
        preset = record('alias-a', base_model_id='different-upstream', meta={})
        plan = app.make_plan_metadata(
            [preset], [{'id': 'different-upstream'}],
            {'alias-a': {'chat_supported': False}})
        self.assertEqual(plan['imports'][0]['id'], 'alias-a')
        self.assertTrue(plan['imports'][0]['meta']['hidden'])
        self.assertNotIn('thinking', plan['imports'][0]['params']['model_controls'])

    def test_unknown_preserves_controls_and_updates_access_and_visibility(self):
        old = record()
        old['params']['model_controls']['reasoning_effort'] = app.build_metadata_controls(
            {'reasoning_effort': {'parameter': 'reasoning_effort', 'options': ['high']}},
            'public')[0]['reasoning_effort']
        for fields in [{}, {'supports_reasoning': False}, {'supports_reasoning': True},
                       {'supported_openai_params': ['reasoning_effort']},
                       {'reasoning_effort_levels': ['bad value']}]:
            with self.subTest(fields=fields):
                plan = self.plan([deployment(mode='chat', **fields)], [old], unhide_chat=True)
                self.assertEqual(plan['public_ids'], ['public'])
                new = plan['imports'][0]
                self.assertEqual(new['params'], old['params'])
                self.assertFalse(new['meta']['hidden'])
                self.assertFalse(new['is_active'])

    def test_explicit_empty_removes_managed_controls(self):
        plan = self.plan([deployment(mode='chat', reasoning_effort_levels=[])])
        self.assertNotIn('thinking', plan['imports'][0]['params']['model_controls'])

    def test_unknown_chat_is_untouched(self):
        for mode in [None, 'future', 'completion']:
            plan = self.plan([deployment(mode=mode, reasoning_effort_levels=['high'])])
            self.assertFalse(plan['imports'])
            self.assertFalse(plan['public_ids'])

    def test_nonchat_hidden_without_disabling_or_revoking(self):
        old = record(meta={}, access_grants=[app.PUBLIC_READ])
        plan = self.plan([deployment(mode='embedding')], [old])
        new = plan['imports'][0]
        self.assertTrue(new['meta']['hidden'])
        self.assertFalse(new['is_active'])
        self.assertEqual(new['access_grants'], [app.PUBLIC_READ])
        self.assertFalse(plan['public_ids'])
        self.assertEqual(app.verify_config([new], plan), [])

    def test_existing_modes(self):
        legacy = app.make_plan_legacy([], [{'id': 'public'}], app.make_legacy_control('max'))
        thinking = legacy['imports'][0]['params']['model_controls']['thinking']
        self.assertEqual(tuple(thinking['options']), app.LEVELS)
        self.assertEqual(thinking['options']['ultra']['params']['reasoning_effort'], 'max')
        # Existing JSON mode remains authoritative, including omitted controls.
        plan = app.make_plan_metadata([record()], [{'id': 'public'}],
                                      {'public': {'chat_supported': True}})
        self.assertNotIn('thinking', plan['imports'][0]['params']['model_controls'])
        metadata = app.parse_metadata('vllm_model_capabilities.example.json')
        self.assertTrue(metadata)


class HttpTests(unittest.TestCase):
    def run_main(self, mode=None, apply=False, discovery=None):
        calls = []
        state = []

        def request(session, base, method, path, **kwargs):
            calls.append((session, base, method, path, deepcopy(session.headers)))
            if path == '/model/info':
                if isinstance(discovery, Exception):
                    raise discovery
                return discovery if discovery is not None else {'data': [
                    deployment(mode='chat', reasoning_effort_levels=['low', 'high'])]}
            if path == '/api/v1/models/all':
                return deepcopy(state)
            if path == '/api/models':
                return {'data': [{'id': 'public'}]}
            if path == '/api/v1/models/import':
                state[:] = deepcopy(kwargs['json']['models'])
                return True
            if path == '/api/v1/models/model/access/update':
                state[0]['access_grants'] = kwargs['json']['access_grants']
                return deepcopy(state[0])
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as directory:
            argv = ['--output-dir', directory]
            if mode:
                argv += ['--mode', mode]
            if mode == 'metadata':
                metadata_path = Path(directory) / 'metadata.json'
                metadata_path.write_text(json.dumps({'models': {'public': {'chat_supported': True}}}))
                argv += ['--metadata-json', str(metadata_path)]
            if apply:
                argv += ['--apply']
            env = {'OPEN_WEBUI_URL': 'https://webui', 'OPEN_WEBUI_API_KEY': 'webui-key',
                   'LITELLM_URL': 'https://litellm/proxy/', 'LITELLM_API_KEY': 'litellm-key'}
            with patch.dict(os.environ, env), patch.object(app, 'request_json', side_effect=request), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    app.main(argv)
                except RuntimeError:
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(calls[0][2:4], ('GET', '/model/info'))
                    self.assertFalse(list(Path(directory).glob('models-backup-*.json')))
                    self.assertFalse((Path(directory) / 'model-controls-bulk.json').exists())
                    raise
            self.assertEqual(len(list(Path(directory).glob('models-backup-*.json'))), 1)
            self.assertTrue((Path(directory) / 'model-controls-bulk.json').exists())
        return calls

    def test_opt_in_and_dry_runs(self):
        for mode in [None, 'legacy', 'metadata', 'litellm']:
            with self.subTest(mode=mode):
                calls = self.run_main(mode)
                self.assertTrue(all(call[2] == 'GET' for call in calls))
                self.assertEqual(sum(call[3] == '/model/info' for call in calls), int(mode == 'litellm'))
                for session, base, _, path, headers in calls:
                    key = 'litellm-key' if path == '/model/info' else 'webui-key'
                    self.assertEqual(headers['Authorization'], 'Bearer ' + key)
                if mode == 'litellm':
                    self.assertIsNot(calls[0][0], calls[1][0])
                    self.assertEqual(calls[0][1], 'https://litellm/proxy')

    def test_discovery_http_response_errors(self):
        for status in [401, 403, 500]:
            response = Mock(status_code=status, text='secret deployment data')
            response.raise_for_status.side_effect = requests.HTTPError('rejected')
            with patch.object(requests.Session, 'request', return_value=response) as req:
                with self.assertRaisesRegex(RuntimeError, f'HTTP {status}') as error:
                    app.discover_litellm('https://litellm', 'key')
                self.assertNotIn('secret', str(error.exception))
                req.assert_called_once_with('GET', 'https://litellm/model/info', timeout=120)
        response = Mock()
        response.json.side_effect = ValueError('not JSON')
        with patch.object(requests.Session, 'request', return_value=response):
            with self.assertRaisesRegex(RuntimeError, 'ValueError'):
                app.discover_litellm('https://litellm', 'key')

    def test_missing_litellm_settings_abort_before_requests(self):
        with patch.dict(os.environ, {'OPEN_WEBUI_URL': 'https://webui',
                                    'OPEN_WEBUI_API_KEY': 'key'}, clear=True), \
                patch.object(app, 'request_json') as req, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                app.main(['--mode', 'litellm', '--apply'])
            req.assert_not_called()

    def test_apply_and_verification(self):
        calls = self.run_main('litellm', apply=True)
        self.assertEqual([c[3] for c in calls if c[2] == 'POST'],
                         ['/api/v1/models/import', '/api/v1/models/model/access/update'])
        self.assertEqual(calls[-1][3], '/api/v1/models/all')

    def test_discovery_failure_before_webui_requests(self):
        for error in [requests.HTTPError('401'), requests.ConnectionError('offline'),
                      requests.Timeout('timeout'), ValueError('invalid JSON')]:
            with self.subTest(error=error):
                with self.assertRaisesRegex(RuntimeError, 'LiteLLM discovery failed'):
                    self.run_main('litellm', apply=True, discovery=error)
        for invalid in [{}, {'data': [None]}]:
            with self.assertRaisesRegex(RuntimeError, 'LiteLLM discovery failed'):
                self.run_main('litellm', apply=True, discovery=invalid)


if __name__ == '__main__':
    unittest.main()
