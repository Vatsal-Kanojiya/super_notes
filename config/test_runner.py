import logging

from django.conf import settings
from django.test.runner import DiscoverRunner


class FastTestRunner(DiscoverRunner):
    """Test runner that swaps production-only settings for test ones.

    Carried over from the reference, where each swap has its story:

    * **Password hasher.** PBKDF2 is slow on purpose; in tests that buys
      nothing. MD5 is used *only* because this runner never runs outside
      the test process. Never put it in settings.PASSWORD_HASHERS.
    * **Cache.** Django does not clear the cache between tests, so a
      throttle count or a rate-limit counter leaks from one test into the
      next. Off by default; a test that is *about* the cache turns it back
      on with override_settings.
    * **Celery eager.** Tasks run inline, so the suite needs no broker and a
      test can assert on what a task did. Set on the Celery app too: it
      reads Django settings once, when first configured.
    * **Providers.** The fake embedding and chat providers, whatever .env
      says, so no test run ever makes a paid network call by accident. The
      real providers' opt-in tests override this themselves.
    """

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
        settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}

        settings.CELERY_TASK_ALWAYS_EAGER = True
        from config.celery import app

        app.conf.task_always_eager = True
        app.conf.task_eager_propagates = True
        # One INFO line per eager task run ("succeeded in 0.01s") is noise here.
        logging.getLogger("celery.app.trace").setLevel(logging.WARNING)

        # Later phases add these settings; set only once they exist.
        for name in ("EMBEDDING_PROVIDER", "CHAT_PROVIDER"):
            if hasattr(settings, name):
                setattr(settings, name, "fake")
