# -*- coding: UTF-8 -*-
from datetime import datetime
from operator import itemgetter

import logging
import pytest
import re
import warnings

# Reference: http://docs.gurock.com/testrail-api2/reference-statuses
TESTRAIL_TEST_STATUS = {
    "passed": 1,
    "blocked": 2,
    "untested": 3,
    "retest": 4,
    "failed": 5,
    "deferred": 6,
    "NA": 7,
    "terraformerror": 8,
}

# Update the mapping for pytest outcomes
PYTEST_TO_TESTRAIL_STATUS = {
    "passed": TESTRAIL_TEST_STATUS["passed"],
    "failed": TESTRAIL_TEST_STATUS["failed"],
    "skipped": TESTRAIL_TEST_STATUS["blocked"],
    "deferred": TESTRAIL_TEST_STATUS["deferred"],
    "NA": TESTRAIL_TEST_STATUS["NA"],
    "terraformerror": TESTRAIL_TEST_STATUS["terraformerror"],
}

DT_FORMAT = '%d-%m-%Y %H:%M:%S'

TESTRAIL_PREFIX = 'testrail'
TESTRAIL_DEFECTS_PREFIX = 'testrail_defects'
ADD_RESULTS_URL = 'add_results_for_cases/{}'
ADD_TESTRUN_URL = 'add_run/{}'
CLOSE_TESTRUN_URL = 'close_run/{}'
CLOSE_TESTPLAN_URL = 'close_plan/{}'
GET_TESTRUN_URL = 'get_run/{}'
GET_TESTPLAN_URL = 'get_plan/{}'
GET_TESTS_URL = 'get_tests/{}'

COMMENT_SIZE_LIMIT = 4000

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
class DeprecatedTestDecorator(DeprecationWarning):
    pass


warnings.simplefilter(action='once', category=DeprecatedTestDecorator, lineno=0)


class pytestrail(object):
    '''
    An alternative to using the testrail function as a decorator for test cases, since py.test may confuse it as a test
    function since it has the 'test' prefix
    '''

    @staticmethod
    def case(*ids):
        """
        Decorator to mark tests with testcase ids.

        ie. @pytestrail.case('C123', 'C12345')

        :return pytest.mark:
        """
        return pytest.mark.testrail(ids=ids)

    @staticmethod
    def defect(*defect_ids):
        """
                Decorator to mark defects with defect ids.

                ie. @pytestrail.defect('PF-513', 'BR-3255')

                :return pytest.mark:
                """
        return pytest.mark.testrail_defects(defect_ids=defect_ids)


def testrail(*ids):
    """
    Decorator to mark tests with testcase ids.

    ie. @testrail('C123', 'C12345')

    :return pytest.mark:
    """
    deprecation_msg = ('pytest_testrail: the @testrail decorator is deprecated and will be removed. Please use the '
                       '@pytestrail.case decorator instead.')
    warnings.warn(deprecation_msg, DeprecatedTestDecorator)
    return pytestrail.case(*ids)


def get_test_outcome(outcome):
    """
    Return numerical value of test outcome.

    :param str outcome: pytest reported test outcome value.
    :returns: int relating to test outcome.
    """
    return PYTEST_TO_TESTRAIL_STATUS[outcome]


def testrun_name():
    """Returns testrun name with timestamp"""
    now = datetime.utcnow()
    return 'Automated Run {}'.format(now.strftime(DT_FORMAT))


def clean_test_ids(test_ids):
    """
    Clean pytest marker containing testrail testcase ids.

    :param list test_ids: list of test_ids.
    :return list ints: contains list of test_ids as ints.
    """
    return [int(re.search('(?P<test_id>[0-9]+$)', test_id).groupdict().get('test_id')) for test_id in test_ids]


def clean_test_defects(defect_ids):
    """
        Clean pytest marker containing testrail defects ids.

        :param list defect_ids: list of defect_ids.
        :return list ints: contains list of defect_ids as ints.
        """
    return [(re.search('(?P<defect_id>.*)', defect_id).groupdict().get('defect_id')) for defect_id in defect_ids]


def get_testrail_keys(items):
    """Return Tuple of Pytest nodes and TestRail ids from pytests markers"""
    testcaseids = []
    for item in items:
        if item.get_closest_marker(TESTRAIL_PREFIX):
            testcaseids.append(
                (
                    item,
                    clean_test_ids(
                        item.get_closest_marker(TESTRAIL_PREFIX).kwargs.get('ids')
                    )
                )
            )
    return testcaseids


class PyTestRailPlugin(object):
    def __init__(self, client, assign_user_id, project_id, suite_id, include_all, cert_check, tr_name,
                 tr_description='', run_id=0, plan_id=0, version='', close_on_complete=False,
                 publish_blocked=True, skip_missing=False, milestone_id=None, custom_comment=None):
        self.assign_user_id = assign_user_id
        self.cert_check = cert_check
        self.client = client
        self.project_id = project_id
        self.results = []
        self.suite_id = suite_id
        self.include_all = include_all
        self.testrun_name = tr_name
        self.testrun_description = tr_description
        self.testrun_id = run_id
        self.testplan_id = plan_id
        self.version = version
        self.close_on_complete = close_on_complete
        self.publish_blocked = publish_blocked
        self.skip_missing = skip_missing
        self.milestone_id = milestone_id
        self.custom_comment = custom_comment

    # pytest hooks

    def pytest_report_header(self, config, startdir):
        """ Add extra-info in header """
        message = 'pytest-testrail: '
        if self.testplan_id:
            message += 'existing testplan #{} selected'.format(self.testplan_id)
        elif self.testrun_id:
            message += 'existing testrun #{} selected'.format(self.testrun_id)
        else:
            message += 'a new testrun will be created'
        return message

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, session, config, items):
        items_with_tr_keys = get_testrail_keys(items)
        tr_keys = [case_id for item in items_with_tr_keys for case_id in item[1]]

        if self.testplan_id and self.is_testplan_available():
            self.testrun_id = 0
        elif self.testrun_id and self.is_testrun_available():
            self.testplan_id = 0
            if self.skip_missing:
                tests_list = [
                    test.get('case_id') for test in self.get_tests(self.testrun_id)
                ]
                for item, case_id in items_with_tr_keys:
                    if not set(case_id).intersection(set(tests_list)):
                        mark = pytest.mark.skip('Test is not present in testrun.')
                        item.add_marker(mark)
        else:
            if self.testrun_name is None:
                self.testrun_name = testrun_name()

            self.create_test_run(
                self.assign_user_id,
                self.project_id,
                self.suite_id,
                self.include_all,
                self.testrun_name,
                tr_keys,
                self.milestone_id,
                self.testrun_description
            )

    @pytest.hookimpl(tryfirst=True, hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        """ Collect result and associated testcases (TestRail) of an execution """
        outcome = yield
        rep = outcome.get_result()
        defectids = None
        if 'callspec' in dir(item):
            test_parametrize = item.callspec.params
        else:
            test_parametrize = None
        comment = rep.longrepr
        if item.get_closest_marker(TESTRAIL_DEFECTS_PREFIX):
            defectids = item.get_closest_marker(TESTRAIL_DEFECTS_PREFIX).kwargs.get('defect_ids')
        if item.get_closest_marker(TESTRAIL_PREFIX):
            testcaseids = item.get_closest_marker(TESTRAIL_PREFIX).kwargs.get('ids')
            if rep.when in ['setup', 'call'] and testcaseids:
                # Check if the test case has already been processed
                if not getattr(item, 'testrail_processed', False):
                    # Mark the test case as processed
                    item.testrail_processed = True
                    if defectids:
                        self.add_result(
                            clean_test_ids(testcaseids),
                            get_test_outcome(outcome.get_result().outcome),
                            comment=comment,
                            duration=rep.duration,
                            defects=str(clean_test_defects(defectids)).replace('[', '').replace(']', '').replace("'", ''),
                            test_parametrize=test_parametrize
                        )
                    else:
                        self.add_result(
                            clean_test_ids(testcaseids),
                            get_test_outcome(outcome.get_result().outcome),
                            comment=comment,
                            duration=rep.duration,
                            test_parametrize=test_parametrize
                        )

    def pytest_sessionfinish(self, session, exitstatus):
        """Publish results in TestRail"""
        print('[{}] Start publishing'.format(TESTRAIL_PREFIX))
        
        if not self.results:
            print('[{}] No test results to publish'.format(TESTRAIL_PREFIX))
            raise Exception('No test results to publish in TestRail')

        tests_list = [str(result['case_id']) for result in self.results]
        print('[{}] Testcases to publish: {}'.format(TESTRAIL_PREFIX, ', '.join(tests_list)))

        if self.testrun_id:
            self.publish_results_for_run(self.testrun_id)
        elif self.testplan_id:
            testruns = self.get_available_testruns(self.testplan_id)
            print('[{}] Testruns to update: {}'.format(TESTRAIL_PREFIX, ', '.join(map(str, testruns))))
            for testrun_id in testruns:
                self.publish_results_for_run(testrun_id)
        else:
            print('[{}] No data published'.format(TESTRAIL_PREFIX))

        if self.close_on_complete:
            if self.testrun_id:
                self.close_test_run(self.testrun_id)
            elif self.testplan_id:
                self.close_test_plan(self.testplan_id)

        print('[{}] End publishing'.format(TESTRAIL_PREFIX))

    def publish_results_for_run(self, testrun_id):
        """Publish results for a specific test run"""
        error = self.add_results(testrun_id)
        if error:
            terraform_errors = self.extract_terraform_errors(error)
            if terraform_errors:
                for test_id, terraform_error in terraform_errors.items():
                    self.add_terraform_error_results(testrun_id, test_id, terraform_error)
                print('[{}] Terraform errors successfully reported for testrun {}'.format(TESTRAIL_PREFIX, testrun_id))
            else:
                print('[{}] Other errors occurred, reporting them for testrun {}'.format(TESTRAIL_PREFIX, testrun_id))
                error_message_parts = error.split(')')
                invalid_test_ids = [part.split('case ')[1].split(' ')[0] for part in error_message_parts if 'case' in part]
                valid_results = [result for result in self.results if result['case_id'] not in invalid_test_ids]
                for invalid_test_id in invalid_test_ids:
                    self.add_error_results(testrun_id, [invalid_test_id], error)
        else:
            print('[{}] Test results successfully published for testrun {}'.format(TESTRAIL_PREFIX, testrun_id))

    def extract_terraform_errors(self, error_message):
        """Extract Terraform errors from the error message"""
        terraform_errors = {}
        for match in re.finditer(r'case (\d+).*?TerraformException: (.+?)\\n', error_message):
            test_id = match.group(1)
            error = match.group(2)
            terraform_errors[test_id] = error
        return terraform_errors

    def add_terraform_error_results(self, testrun_id, test_id, terraform_error):
        """Add results for Terraform errors"""
        status_id = '8'  # Update status_id for Terraform errors
        comment = "Terraform Exception: {}".format(terraform_error)  # Modify comment to reflect Terraform error
        self.client.send_post(
            'add_result_for_case/{}/{}'.format(testrun_id, test_id),
            {'status_id': status_id, 'comment': comment}
        )


    # plugin

    def add_result(self, test_ids, status, comment='', defects=None, duration=0, test_parametrize=None):
        """
        Add a new result to results dict to be submitted at the end.

        :param list test_parametrize: Add test parametrize to test result
        :param defects: Add defects to test result
        :param list test_ids: list of test_ids.
        :param int status: status code of test (pass or fail).
        :param comment: None or a failure representation.
        :param duration: Time it took to run just the test.
        """
        for test_id in test_ids:
            # Convert comment to string if it's not already
            if not isinstance(comment, str):
                comment = str(comment)

            # Update status code from 5 to 6 for Terraform errors
            if status == 5 and "TerraformException" in comment:
                status = 8

            data = {
                'case_id': test_id,
                'status_id': status,
                'comment': comment,
                'duration': duration,
                'defects': defects,
                'test_parametrize': test_parametrize
            }
            self.results.append(data)
            logger.info("Added result for case {}: status={}, comment={}, defects={}, duration={}, test_parametrize={}".format(
                test_id, status, comment, defects, duration, test_parametrize))


    def add_error_results(self, testrun_id, invalid_test_ids, error):
        """
        Add error results for test cases excluding the invalid test case IDs.

        :param testrun_id: ID of the test run.
        :param invalid_test_ids: List of invalid test case IDs.
        :param error: Error message.
        """
        # Log the error message and invalid test case IDs
        logger.error('[{}] Info: Testcases not published for the following reason: "{}"'.format(TESTRAIL_PREFIX, error))
        logger.error('[{}] Invalid test case IDs: {}'.format(TESTRAIL_PREFIX, invalid_test_ids))

        # Remove the leading "C" character from invalid test case IDs if the first character is "C"
        invalid_test_ids = [id[1:] if id.startswith('C') else id for id in invalid_test_ids]
        valid_results = [result for result in self.results if result['case_id'] not in invalid_test_ids]
        data = {'results': []}
        for result in valid_results:
            entry = {
                'case_id': result['case_id'],
                'status_id': TESTRAIL_TEST_STATUS["failed"],
                'comment': error,
                'defects': ''
            }
            # Directly call the TestRail API to add the result to the test run
            response = self.client.send_post(
                ADD_RESULTS_URL.format(testrun_id),
                {'results': [entry]},
                cert_check=self.cert_check
            )

            logger.info("Response received for error result: {}".format(response))

            error_response = self.client.get_error(response)

            if error_response:
                logger.error('[{}] Error adding result for case {}: "{}"'.format(TESTRAIL_PREFIX, result['case_id'], error_response))

    def add_results(self, testrun_id):
        """
        Add results one by one to improve error handling.

        :param testrun_id: Id of the testrun to feed

        """
        # unicode converter for compatibility of python 2 and 3
        try:
            converter = unicode
        except NameError:
            converter = lambda s, c: str(bytes(s, "utf-8"), c)
        # Results are sorted by 'case_id' and by 'status_id' (worst result at the end)
        self.results.sort(key=itemgetter("case_id"))

        # Manage case of "blocked" testcases
        if self.publish_blocked is False:
            logger.info(
                '[{}] Option "Don\'t publish blocked testcases" activated'.format(
                    TESTRAIL_PREFIX
                )
            )
            blocked_tests_list = [
                test.get("case_id")
                for test in self.get_tests(testrun_id)
                if test.get("status_id") == TESTRAIL_TEST_STATUS["blocked"]
            ]
            logger.info(
                "[{}] Blocked testcases excluded: {}".format(
                    TESTRAIL_PREFIX, ", ".join(str(elt) for elt in blocked_tests_list)
                )
            )
            self.results = [
                result
                for result in self.results
                if result.get("case_id") not in blocked_tests_list
            ]

        # prompt enabling include all test cases from test suite when creating test run
        if self.include_all:
            logger.info(
                '[{}] Option "Include all testcases from test suite for test run" activated'.format(
                    TESTRAIL_PREFIX
                )
            )

        # Publish results
        data = {"results": []}
        for result in self.results:
            entry = {
                "status_id": result["status_id"],
                "case_id": result["case_id"],
                "defects": result["defects"],
            }
            if self.version:
                entry["version"] = self.version
            comment = result.get("comment", "")
            test_parametrize = result.get("test_parametrize", "")
            entry["comment"] = ""
            if test_parametrize:
                entry["comment"] += "# Test parametrize: #\n"
                entry["comment"] += str(test_parametrize) + "\n\n"
            if comment:
                if self.custom_comment:
                    entry["comment"] += self.custom_comment + "\n"
                    # Indent text to avoid string formatting by TestRail. Limit size of comment.
                    entry["comment"] += "# Pytest result: #\n"
                    entry["comment"] += (
                        "Log truncated\n...\n"
                        if len(str(comment)) > COMMENT_SIZE_LIMIT
                        else ""
                    )
                    entry["comment"] += "    " + converter(str(comment), "utf-8")[
                        -COMMENT_SIZE_LIMIT:
                    ].replace(
                        "\n", "\n    "
                    )  # noqa
                else:
                    # Indent text to avoid string formatting by TestRail. Limit size of comment.
                    entry["comment"] += "# Pytest result: #\n"
                    entry["comment"] += (
                        "Log truncated\n...\n"
                        if len(str(comment)) > COMMENT_SIZE_LIMIT
                        else ""
                    )
                    entry["comment"] += "    " + converter(str(comment), "utf-8")[
                        -COMMENT_SIZE_LIMIT:
                    ].replace(
                        "\n", "\n    "
                    )  # noqa
            elif comment == "":
                entry["comment"] = self.custom_comment
            duration = result.get("duration")
            if duration:
                duration = (
                    1 if (duration < 1) else int(round(duration))
                )  # TestRail API doesn't manage milliseconds
                entry["elapsed"] = str(duration) + "s"
            data["results"].append(entry)

        # Send the HTTP POST request outside the loop
        response = self.client.send_post(
            ADD_RESULTS_URL.format(testrun_id), data, cert_check=self.cert_check
        )

        logger.info("Response received: {}".format(response))

        for resp in response:
            comment = resp.get("comment", "")
            if "TerraformException" in comment:
                status_id = resp.get("status_id")
                self.add_terraform_error_results(testrun_id, status_id, comment)

        error = self.client.get_error(resp)
        if error:
            logger.error("Error in sending results to TestRail. Status code: {}".format(resp.status_code))
            error = self.client.get_error(resp)
            return error

    def create_test_run(self, assign_user_id, project_id, suite_id, include_all,
                        testrun_name, tr_keys, milestone_id, description=''):
        """
        Create testrun with ids collected from markers.

        :param tr_keys: collected testrail ids.
        """
        data = {
            'suite_id': suite_id,
            'name': testrun_name,
            'description': description,
            'assignedto_id': assign_user_id,
            'include_all': include_all,
            'case_ids': tr_keys,
            'milestone_id': milestone_id
        }

        response = self.client.send_post(
            ADD_TESTRUN_URL.format(project_id),
            data,
            cert_check=self.cert_check
        )
        error = self.client.get_error(response)
        if error:
            print('[{}] Failed to create testrun: "{}"'.format(TESTRAIL_PREFIX, error))
        else:
            self.testrun_id = response['id']
            print('[{}] New testrun created with name "{}" and ID={}'.format(TESTRAIL_PREFIX,
                                                                             testrun_name,
                                                                             self.testrun_id))

    def close_test_run(self, testrun_id):
        """
        Closes testrun.

        """
        response = self.client.send_post(
            CLOSE_TESTRUN_URL.format(testrun_id),
            data={},
            cert_check=self.cert_check
        )
        error = self.client.get_error(response)
        if error:
            print('[{}] Failed to close test run: "{}"'.format(TESTRAIL_PREFIX, error))
        else:
            print('[{}] Test run with ID={} was closed'.format(TESTRAIL_PREFIX, self.testrun_id))

    def close_test_plan(self, testplan_id):
        """
        Closes testrun.

        """
        response = self.client.send_post(
            CLOSE_TESTPLAN_URL.format(testplan_id),
            data={},
            cert_check=self.cert_check
        )
        error = self.client.get_error(response)
        if error:
            print('[{}] Failed to close test plan: "{}"'.format(TESTRAIL_PREFIX, error))
        else:
            print('[{}] Test plan with ID={} was closed'.format(TESTRAIL_PREFIX, self.testplan_id))

    def is_testrun_available(self):
        """
        Ask if testrun is available in TestRail.

        :return: True if testrun exists AND is open
        """
        response = self.client.send_get(
            GET_TESTRUN_URL.format(self.testrun_id),
            cert_check=self.cert_check
        )
        error = self.client.get_error(response)
        if error:
            print('[{}] Failed to retrieve testrun: "{}"'.format(TESTRAIL_PREFIX, error))
            return False

        return response['is_completed'] is False

    def is_testplan_available(self):
        """
        Ask if testplan is available in TestRail.

        :return: True if testplan exists AND is open
        """
        response = self.client.send_get(
            GET_TESTPLAN_URL.format(self.testplan_id),
            cert_check=self.cert_check
        )
        error = self.client.get_error(response)
        if error:
            print('[{}] Failed to retrieve testplan: "{}"'.format(TESTRAIL_PREFIX, error))
            return False

        return response['is_completed'] is False

    def get_available_testruns(self, plan_id):
        """
        :return: a list of available testruns associated to a testplan in TestRail.

        """
        testruns_list = []
        response = self.client.send_get(
            GET_TESTPLAN_URL.format(plan_id),
            cert_check=self.cert_check
        )
        error = self.client.get_error(response)
        if error:
            print('[{}] Failed to retrieve testplan: "{}"'.format(TESTRAIL_PREFIX, error))
        else:
            for entry in response['entries']:
                for run in entry['runs']:
                    if not run['is_completed']:
                        testruns_list.append(run['id'])
        return testruns_list

    def get_tests(self, run_id):
        """
        :return: the list of tests containing in a testrun.

        """
        response = self.client.send_get(
            GET_TESTS_URL.format(run_id),
            cert_check=self.cert_check
        )
        error = self.client.get_error(response)
        if error:
            print('[{}] Failed to get tests: "{}"'.format(TESTRAIL_PREFIX, error))
            return None
        return response
