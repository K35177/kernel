import asyncio
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import bot
from kernel import config
from kernel.config import ConfigurationError, Settings
from tests.helpers import TOKEN

VALID = {'BOT_TOKEN': TOKEN, 'OWNER_ID': '424242001', 'ROOT_GROUP_ID': '-1004242420001'}


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root_patch = patch.object(config, 'PROJECT_ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.temp.cleanup)

    def load(self, values):
        with patch.dict(os.environ, values, clear=True):
            return Settings.from_env()

    def test_missing_required_values(self):
        for name in VALID:
            with self.subTest(variable=name):
                values = VALID.copy()
                del values[name]
                with self.assertRaisesRegex(ConfigurationError, name + ' не задан'):
                    self.load(values)

    def test_empty_required_values(self):
        for name in VALID:
            for empty in ('', '  \t'):
                with self.subTest(variable=name, blank=repr(empty)):
                    with self.assertRaisesRegex(ConfigurationError, name + ' пуст'):
                        self.load({**VALID, name: empty})

    def test_invalid_tokens_are_rejected_without_disclosing_them(self):
        for token in ('example-secret-sentinel', '123:', ':secret-sentinel', '123 :secret-sentinel'):
            with self.subTest(case=token.index(':') if ':' in token else 'no separator'):
                with self.assertRaises(ConfigurationError) as caught:
                    self.load({**VALID, 'BOT_TOKEN': token})
                self.assertIn('BOT_TOKEN', str(caught.exception))
                self.assertIn('некорректный формат', str(caught.exception))
                self.assertNotIn(token, str(caught.exception))
                self.assertNotIn('secret-sentinel', str(caught.exception))

    def test_invalid_and_wrong_sign_ids_have_safe_diagnostics(self):
        for name, samples in [('OWNER_ID', ('private-id-sentinel', '1.5', '0', '-1')),
                              ('ROOT_GROUP_ID', ('private-id-sentinel', '1.5', '0', '1'))]:
            for value in samples:
                with self.subTest(variable=name, value_type='numeric' if value.lstrip('-').isdigit() else 'text'):
                    with self.assertRaises(ConfigurationError) as caught:
                        self.load({**VALID, name: value})
                    self.assertIn(name, str(caught.exception))
                    self.assertNotIn('private-id-sentinel', str(caught.exception))

    def test_absent_optional_values_keep_existing_defaults(self):
        settings = self.load(VALID)
        self.assertEqual(settings.token, TOKEN)
        self.assertEqual(settings.owner_id, 424242001)
        self.assertEqual(settings.root_group_id, -1004242420001)
        self.assertEqual(settings.timezone, 'Europe/Moscow')
        self.assertEqual(settings.database_path, self.root / 'data/kernel.sqlite3')
        self.assertIsNone(settings.mini_app_url)
        self.assertFalse((self.root / 'data').exists())

    def test_invalid_timezones_have_safe_diagnostics(self):
        for value in ('', 'nonexistent-zone-sentinel', '../private-zone'):
            with self.subTest(case='empty' if not value else 'invalid'):
                with self.assertRaisesRegex(ConfigurationError, 'TIMEZONE') as caught:
                    self.load({**VALID, 'TIMEZONE': value})
                if value:
                    self.assertNotIn(value, str(caught.exception))

    def test_invalid_urls_and_embedded_credentials_are_not_printed(self):
        for value in ('http://example.test', 'https://[', 'https://example.test:bad',
                      'https://private-login:private-password@example.test'):
            with self.subTest(case='invalid url'):
                with self.assertRaisesRegex(ConfigurationError, 'MINI_APP_URL') as caught:
                    self.load({**VALID, 'MINI_APP_URL': value})
                self.assertNotIn(value, str(caught.exception))
                self.assertNotIn('private-password', str(caught.exception))

    def test_blank_mini_app_url_remains_optional(self):
        self.assertIsNone(self.load({**VALID, 'MINI_APP_URL': '  '}).mini_app_url)

    def test_invalid_database_paths(self):
        for value in ('', '  ', '~unknown-kernel-config-user/file.sqlite3'):
            with self.subTest(case='empty' if not value.strip() else 'invalid path'):
                with self.assertRaisesRegex(ConfigurationError, 'DATABASE_PATH'):
                    self.load({**VALID, 'DATABASE_PATH': value})

    def test_null_in_dotenv_is_reported_without_value_and_file_is_preserved(self):
        env = self.root / '.env'
        content = '\n'.join(f'{k}={v}' for k, v in VALID.items()) + '\nDATABASE_PATH=private\x00path\n'
        env.write_text(content)
        with self.assertRaises(ConfigurationError) as caught:
            self.load({})
        self.assertNotIn('private', str(caught.exception))
        self.assertEqual(env.read_text(), content)

    def test_absolute_database_path_is_preserved(self):
        path = self.root / 'custom.sqlite3'
        self.assertEqual(self.load({**VALID, 'DATABASE_PATH': str(path)}).database_path, path)

    def test_dotenv_is_read_without_rewriting_bytes_or_metadata(self):
        env = self.root / '.env'
        content = '# user settings\n' + '\n'.join(f'{k}={v}' for k, v in VALID.items()) + '\n'
        env.write_text(content)
        before = env.stat()
        settings = self.load({})
        self.assertEqual(settings.token, TOKEN)
        self.assertEqual(env.read_text(), content)
        self.assertEqual((env.stat().st_mtime_ns, env.stat().st_mode), (before.st_mtime_ns, before.st_mode))

    def test_process_environment_keeps_priority_over_dotenv(self):
        env = self.root / '.env'
        content = 'BOT_TOKEN=invalid-file-sentinel\nOWNER_ID=1\nROOT_GROUP_ID=-1\n'
        env.write_text(content)
        settings = self.load(VALID)
        self.assertEqual(settings.token, TOKEN)
        self.assertEqual(settings.owner_id, 424242001)
        self.assertEqual(settings.root_group_id, -1004242420001)
        self.assertEqual(env.read_text(), content)

    def test_invalid_process_token_does_not_fall_back_to_working_file(self):
        env = self.root / '.env'
        content = '\n'.join(f'{k}={v}' for k, v in VALID.items()) + '\n'
        env.write_text(content)
        with self.assertRaisesRegex(ConfigurationError, 'окружения процесса') as caught:
            self.load({'BOT_TOKEN': 'invalid-process-sentinel'})
        self.assertNotIn('invalid-process-sentinel', str(caught.exception))
        self.assertEqual(env.read_text(), content)

    def test_invalid_file_token_is_not_automatically_repaired(self):
        env = self.root / '.env'
        content = 'BOT_TOKEN=invalid-file-sentinel\nOWNER_ID=1\nROOT_GROUP_ID=-1\n'
        env.write_text(content)
        with self.assertRaisesRegex(ConfigurationError, 'BOT_TOKEN из .env'):
            self.load({})
        self.assertEqual(env.read_text(), content)

    def test_missing_dotenv_is_not_created_on_success_or_failure(self):
        self.load(VALID)
        self.assertFalse((self.root / '.env').exists())
        with self.assertRaises(ConfigurationError):
            self.load({})
        self.assertFalse((self.root / '.env').exists())

    def test_unreadable_dotenv_diagnostic_hides_error_details(self):
        with patch.object(config, 'load_dotenv', side_effect=OSError('private-file-sentinel')):
            with self.assertRaises(ConfigurationError) as caught:
                self.load(VALID)
        self.assertNotIn('private-file-sentinel', str(caught.exception))

    def test_settings_repr_never_contains_token(self):
        self.assertNotIn(TOKEN, repr(self.load(VALID)))

    def test_configuration_failure_exits_before_bot_database_or_network(self):
        cases = ({}, {**VALID, 'BOT_TOKEN': ''}, {**VALID, 'BOT_TOKEN': 'private-startup-sentinel'},
                 {**VALID, 'OWNER_ID': 'private-owner-sentinel'})
        for values in cases:
            with self.subTest(case='invalid startup configuration'):
                stderr = io.StringIO()
                with patch.dict(os.environ, values, clear=True), redirect_stderr(stderr), \
                     patch.object(bot, 'Bot') as bot_constructor, patch.object(bot, 'Store') as store_constructor:
                    with self.assertRaises(SystemExit) as caught:
                        asyncio.run(bot.main())
                self.assertEqual(caught.exception.code, 2)
                self.assertIn('Ошибка конфигурации:', stderr.getvalue())
                self.assertNotIn('Traceback', stderr.getvalue())
                self.assertNotIn('private-', stderr.getvalue())
                self.assertNotIn(TOKEN, stderr.getvalue())
                bot_constructor.assert_not_called()
                store_constructor.assert_not_called()


class GitIgnoreTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('git'), 'git is required to verify actual ignore rules')
    def test_secret_and_sqlite_paths_are_ignored_but_example_is_not(self):
        # Verify Git's actual matching in an isolated repository, never the
        # user's read-only .git or any real secret/database contents.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = Path(__file__).resolve().parent.parent / '.gitignore'
            shutil.copyfile(source, root / '.gitignore')
            subprocess.run(['git', 'init', '--quiet', str(root)], check=True, capture_output=True)
            paths = ['.env', '.env.local', 'data/kernel.sqlite3', 'custom.sqlite',
                     'custom.sqlite-wal', 'custom.sqlite-shm', 'custom.sqlite3-wal',
                     'custom.sqlite3-shm', 'custom.db', 'custom.db-journal', 'backups/snapshot/.env']
            result = subprocess.run(['git', '-C', str(root), 'check-ignore', '--no-index', '--stdin'],
                input='\n'.join(paths) + '\n', text=True, capture_output=True, check=True)
            self.assertEqual(set(result.stdout.splitlines()), set(paths))
            example = subprocess.run(['git', '-C', str(root), 'check-ignore', '--no-index', '.env.example'],
                text=True, capture_output=True)
            self.assertEqual(example.returncode, 1)
