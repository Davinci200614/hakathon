"""Ensure benchmark scoring catches the actual conversation failure modes."""
import unittest
from scripts.check_groq import evaluate
from scripts.groq_cases import CASES
from app.understanding import Understanding, Facts


class GroqBenchmarkTests(unittest.TestCase):
    def case(self,name):
        return next(item for item in CASES if item['id']==name)

    def test_mixed_acknowledgement_cannot_pass_as_confirmation(self):
        result=Understanding(task='machine',action='confirm',facts=Facts(machine_type='tractor',area_hectares=2))
        failures=evaluate(self.case('mixed_yes_topic_switch'),result)
        self.assertIn('task',failures)
        self.assertIn('unsafe_action',failures)
        self.assertIn('invented_or_copied_machine_type',failures)

    def test_correction_must_not_copy_old_facts(self):
        result=Understanding(task='machine',action='correct',facts=Facts(location='بني مسكين',machine_type='tractor'))
        self.assertIn('invented_or_copied_machine_type',evaluate(self.case('location_correction'),result))
        correct=Understanding(task='machine',action='correct',facts=Facts(location='بني مسكين'))
        self.assertEqual(evaluate(self.case('location_correction'),correct),[])

    def test_contact_denial_requires_explicit_invalidation(self):
        incomplete=Understanding(task='post_worker',action='correct')
        self.assertIn('clear_fields',evaluate(self.case('contact_denial'),incomplete))
        complete=Understanding(task='post_worker',action='correct',clear_fields=['contact'])
        self.assertEqual(evaluate(self.case('contact_denial'),complete),[])


if __name__=='__main__':unittest.main()
