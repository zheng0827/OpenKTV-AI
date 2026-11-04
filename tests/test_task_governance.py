import unittest

from core.task_governance import make_retry_task, normalize_priority, queue_state, sort_pending_tasks


class TaskGovernanceTests(unittest.TestCase):
    def test_priority_normalization_is_clamped(self):
        self.assertEqual(normalize_priority(300), 100)
        self.assertEqual(normalize_priority(-300), -100)
        self.assertEqual(normalize_priority("x"), 0)

    def test_pending_tasks_are_sorted_by_priority_then_created_time(self):
        tasks = [
            {"task_id": 1, "priority": 0, "status": "pending", "created_at": 100},
            {"task_id": 2, "priority": 10, "status": "pending", "created_at": 200},
            {"task_id": 3, "priority": 10, "status": "pending", "created_at": 50},
        ]
        ordered = sort_pending_tasks(tasks)
        self.assertEqual([item["task_id"] for item in ordered], [3, 2, 1])

    def test_queue_state_reports_running_pending_and_recent(self):
        tasks = [
            {"task_id": 1, "priority": 0, "status": "pending", "label": "A", "created_at": 1},
            {"task_id": 2, "priority": 2, "status": "running", "label": "B", "created_at": 2, "started_at": 3, "progress": {"current": 1}},
            {"task_id": 3, "priority": 1, "status": "failed", "label": "C", "finished_at": 9, "summary": {"failed": ["x"]}},
        ]
        payload = queue_state(tasks, active_task_id=2)
        self.assertEqual(payload["running"]["task_id"], 2)
        self.assertEqual(payload["pending"][0]["task_id"], 1)
        self.assertEqual(payload["recent"][0]["task_id"], 3)

    def test_make_retry_task_resets_runtime_fields(self):
        original = {
            "task_id": 5,
            "priority": 8,
            "status": "failed",
            "tasks": [{"url": "https://youtu.be/x"}],
            "options": {},
            "retries": 1,
            "progress": {"current": 1},
            "summary": {"failed": ["S"]},
        }
        retry = make_retry_task(original, next_task_id=6)
        self.assertEqual(retry["task_id"], 6)
        self.assertEqual(retry["status"], "pending")
        self.assertEqual(retry["retries"], 2)
        self.assertEqual(retry["retry_of"], 5)
        self.assertEqual(retry["progress"], {})


if __name__ == "__main__":
    unittest.main()
