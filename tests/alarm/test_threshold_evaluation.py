# -*- encoding: utf-8 -*-
#
# Copyright © 2013 Red Hat, Inc
#
# Author: Eoghan Glynn <eglynn@redhat.com>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""Tests for ceilometer/alarm/threshold_evaluation.py
"""
import mock
import uuid

from ceilometer.alarm import threshold_evaluation
from ceilometer.storage import models
from ceilometer.tests import base
from ceilometerclient import exc
from ceilometerclient.v2 import statistics


class TestEvaluate(base.TestCase):
    def setUp(self):
        super(TestEvaluate, self).setUp()
        self.api_client = mock.Mock()
        self.notifier = mock.MagicMock()
        self.alarms = [
            models.Alarm(name='instance_running_hot',
                         counter_name='cpu_util',
                         comparison_operator='gt',
                         threshold=80.0,
                         evaluation_periods=5,
                         statistic='avg',
                         user_id='foobar',
                         project_id='snafu',
                         period=60,
                         alarm_id=str(uuid.uuid4()),
                         matching_metadata={'resource_id':
                                            'my_instance'}),
            models.Alarm(name='group_running_idle',
                         counter_name='cpu_util',
                         comparison_operator='le',
                         threshold=10.0,
                         statistic='max',
                         evaluation_periods=4,
                         user_id='foobar',
                         project_id='snafu',
                         period=300,
                         alarm_id=str(uuid.uuid4()),
                         matching_metadata={'metadata.user_metadata.AS':
                                            'my_group'}),
        ]
        self.evaluator = threshold_evaluation.Evaluator(self.notifier)
        self.evaluator.assign_alarms(self.alarms)

    @staticmethod
    def _get_stat(attr, value):
        return statistics.Statistics(None, {attr: value})

    def _set_all_alarms(self, state):
        for alarm in self.alarms:
            alarm.state = state

    def _assert_all_alarms(self, state):
        for alarm in self.alarms:
            self.assertEqual(alarm.state, state)

    def test_retry_transient_api_failure(self):
        with mock.patch('ceilometerclient.client.get_client',
                        return_value=self.api_client):
            broken = exc.CommunicationError(message='broken')
            avgs = [self._get_stat('avg', self.alarms[0].threshold - v)
                    for v in xrange(5)]
            maxs = [self._get_stat('max', self.alarms[1].threshold + v)
                    for v in xrange(1, 4)]
            self.api_client.statistics.list.side_effect = [broken,
                                                           broken,
                                                           avgs,
                                                           maxs]
            self.evaluator.evaluate()
            self._assert_all_alarms('insufficient data')
            self.evaluator.evaluate()
            self._assert_all_alarms('ok')

    def test_simple_insufficient(self):
        self._set_all_alarms('ok')
        with mock.patch('ceilometerclient.client.get_client',
                        return_value=self.api_client):
            self.api_client.statistics.list.return_value = []
            self.evaluator.evaluate()
            self._assert_all_alarms('insufficient data')
            expected = [mock.call(alarm.alarm_id, state='insufficient data')
                        for alarm in self.alarms]
            update_calls = self.api_client.alarms.update.call_args_list
            self.assertEqual(update_calls, expected)
            expected = [mock.call(alarm,
                                  'insufficient data',
                                  ('%d datapoints are unknown' %
                                   alarm.evaluation_periods))
                        for alarm in self.alarms]
            self.assertEqual(self.notifier.notify.call_args_list, expected)

    def test_disabled_is_skipped(self):
        self._set_all_alarms('ok')
        self.alarms[1].enabled = False
        with mock.patch('ceilometerclient.client.get_client',
                        return_value=self.api_client):
            self.api_client.statistics.list.return_value = []
            self.evaluator.evaluate()
            self.assertEqual(self.alarms[0].state, 'insufficient data')
            self.assertEqual(self.alarms[1].state, 'ok')
            self.api_client.alarms.update.assert_called_once_with(
                self.alarms[0].alarm_id,
                state='insufficient data'
            )
            self.notifier.notify.assert_called_once_with(
                self.alarms[0],
                'insufficient data',
                mock.ANY
            )

    def test_simple_alarm_trip(self):
        self._set_all_alarms('ok')
        with mock.patch('ceilometerclient.client.get_client',
                        return_value=self.api_client):
            avgs = [self._get_stat('avg', self.alarms[0].threshold + v)
                    for v in xrange(1, 6)]
            maxs = [self._get_stat('max', self.alarms[1].threshold - v)
                    for v in xrange(4)]
            self.api_client.statistics.list.side_effect = [avgs, maxs]
            self.evaluator.evaluate()
            self._assert_all_alarms('alarm')
            expected = [mock.call(alarm.alarm_id, state='alarm')
                        for alarm in self.alarms]
            update_calls = self.api_client.alarms.update.call_args_list
            self.assertEqual(update_calls, expected)
            reasons = ['Transition to alarm due to 5 samples outside'
                       ' threshold, most recent: 85.0',
                       'Transition to alarm due to 4 samples outside'
                       ' threshold, most recent: 7.0']
            expected = [mock.call(alarm, 'alarm', reason)
                        for alarm, reason in zip(self.alarms, reasons)]
            self.assertEqual(self.notifier.notify.call_args_list, expected)

    def test_simple_alarm_clear(self):
        self._set_all_alarms('alarm')
        with mock.patch('ceilometerclient.client.get_client',
                        return_value=self.api_client):
            avgs = [self._get_stat('avg', self.alarms[0].threshold - v)
                    for v in xrange(5)]
            maxs = [self._get_stat('max', self.alarms[1].threshold + v)
                    for v in xrange(1, 5)]
            self.api_client.statistics.list.side_effect = [avgs, maxs]
            self.evaluator.evaluate()
            self._assert_all_alarms('ok')
            expected = [mock.call(alarm.alarm_id, state='ok')
                        for alarm in self.alarms]
            update_calls = self.api_client.alarms.update.call_args_list
            self.assertEqual(update_calls, expected)
            reasons = ['Transition to ok due to 5 samples inside'
                       ' threshold, most recent: 76.0',
                       'Transition to ok due to 4 samples inside'
                       ' threshold, most recent: 14.0']
            expected = [mock.call(alarm, 'ok', reason)
                        for alarm, reason in zip(self.alarms, reasons)]
            self.assertEqual(self.notifier.notify.call_args_list, expected)

    def test_equivocal_from_known_state(self):
        self._set_all_alarms('ok')
        with mock.patch('ceilometerclient.client.get_client',
                        return_value=self.api_client):
            avgs = [self._get_stat('avg', self.alarms[0].threshold + v)
                    for v in xrange(5)]
            maxs = [self._get_stat('max', self.alarms[1].threshold - v)
                    for v in xrange(-1, 3)]
            self.api_client.statistics.list.side_effect = [avgs, maxs]
            self.evaluator.evaluate()
            self._assert_all_alarms('ok')
            self.assertEqual(self.api_client.alarms.update.call_args_list,
                             [])
            self.assertEqual(self.notifier.notify.call_args_list, [])

    def test_equivocal_from_unknown(self):
        self._set_all_alarms('insufficient data')
        with mock.patch('ceilometerclient.client.get_client',
                        return_value=self.api_client):
            avgs = [self._get_stat('avg', self.alarms[0].threshold + v)
                    for v in xrange(1, 6)]
            maxs = [self._get_stat('max', self.alarms[1].threshold - v)
                    for v in xrange(4)]
            self.api_client.statistics.list.side_effect = [avgs, maxs]
            self.evaluator.evaluate()
            self._assert_all_alarms('alarm')
            expected = [mock.call(alarm.alarm_id, state='alarm')
                        for alarm in self.alarms]
            update_calls = self.api_client.alarms.update.call_args_list
            self.assertEqual(update_calls, expected)
            reasons = ['Transition to alarm due to 5 samples outside'
                       ' threshold, most recent: 85.0',
                       'Transition to alarm due to 4 samples outside'
                       ' threshold, most recent: 7.0']
            expected = [mock.call(alarm, 'alarm', reason)
                        for alarm, reason in zip(self.alarms, reasons)]
            self.assertEqual(self.notifier.notify.call_args_list, expected)
