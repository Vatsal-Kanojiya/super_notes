from django.contrib.auth import get_user_model
from django.test import TestCase

User = get_user_model()


class UserModelTests(TestCase):
    def test_user_is_keyed_by_lowercased_email_with_no_usable_password(self):
        user = User.objects.create_user("Someone@Example.COM")
        self.assertEqual(user.email, "someone@example.com")
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.plan, User.Plan.FREE)
        self.assertEqual(user.notes_revision, 0)
        self.assertEqual(str(user), "someone@example.com")

    def test_superuser_has_a_password_for_the_admin(self):
        admin = User.objects.create_superuser("admin@example.com", "a-long-admin-password")
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.check_password("a-long-admin-password"))

    def test_email_is_required(self):
        with self.assertRaises(ValueError):
            User.objects.create_user("")

    def test_admin_changelist_renders(self):
        admin = User.objects.create_superuser("admin@example.com", "a-long-admin-password")
        self.client.force_login(admin)
        response = self.client.get("/admin/accounts/user/")
        self.assertEqual(response.status_code, 200)
