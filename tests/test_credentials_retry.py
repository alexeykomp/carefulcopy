import threading
import time
import unittest

from carefulcopy import (AuthenticationError, BatchedFailurePrompt, CallbackCredentials, ChainCredentials,
                         Credentials, EnvCredentials, KeyringCredentials, PromptCredentials,
                         StaticCredentials)

import helpers  # noqa: F401


class FakeKeyring:
    def __init__(self):
        self.store, self.sets, self.deletes = {}, 0, 0

    def get_password(self, service, key):
        return self.store.get((service, key))

    def set_password(self, service, key, value):
        self.sets += 1
        self.store[(service, key)] = value

    def delete_password(self, service, key):
        self.deletes += 1
        self.store.pop((service, key), None)


class CredentialTests(unittest.TestCase):
    def test_repr_hides_password(self):
        self.assertNotIn("hunter2", repr(Credentials("bob", "hunter2")))

    def test_static_with_per_host_override(self):
        p = StaticCredentials("a", "1", per_host={"nas2": Credentials("b", "2")})
        self.assertEqual(p.get("nas"), Credentials("a", "1"))
        self.assertEqual(p.get("nas2"), Credentials("b", "2"))
        self.assertIsNone(StaticCredentials().get("x"))

    def test_env(self):
        env = {"CAREFULCOPY_SMB_USERNAME": "u", "CAREFULCOPY_SMB_PASSWORD": "p"}
        self.assertEqual(EnvCredentials(environ=env).get("h"), Credentials("u", "p"))
        self.assertIsNone(EnvCredentials(environ={}).get("h"))

    def test_chain_first_hit_and_hooks_reach_everyone(self):
        calls = []

        class P:
            def __init__(self, name, creds):
                self.name, self.creds = name, creds

            def get(self, host):
                return self.creds

            def remember(self, host, creds):
                calls.append(("remember", self.name))

            def invalidate(self, host):
                calls.append(("invalidate", self.name))
        chain = ChainCredentials(P("a", None), P("b", Credentials("u", "p")), P("c", Credentials("x", "y")))
        self.assertEqual(chain.get("h"), Credentials("u", "p"))
        chain.remember("h", Credentials("u", "p"))
        chain.invalidate("h")
        self.assertEqual(len(calls), 6)

    def test_callback_is_serialised(self):
        active, peak = [0], [0]

        def ask(host):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            time.sleep(0.02)
            active[0] -= 1
            return Credentials("u", "p")
        p = CallbackCredentials(ask)
        ts = [threading.Thread(target=p.get, args=("h",)) for _ in range(5)]
        [t.start() for t in ts]
        [t.join(5) for t in ts]
        self.assertEqual(peak[0], 1)

    def test_prompt(self):
        p = PromptCredentials("dflt", input_fn=lambda _: "", getpass_fn=lambda _: "pw")
        self.assertEqual(p.get("h"), Credentials("dflt", "pw"))

    def test_prompt_cancel_and_empty(self):
        def eof(_):
            raise EOFError
        with self.assertRaises(AuthenticationError):
            PromptCredentials(input_fn=eof).get("h")
        with self.assertRaises(AuthenticationError):
            PromptCredentials(input_fn=lambda _: "", getpass_fn=lambda _: "").get("h")

    def test_keyring_lookup_fallback_remember_invalidate(self):
        kr = FakeKeyring()
        fallback = StaticCredentials("fb", "fbpw")
        p = KeyringCredentials(fallback, keyring_module=kr)
        self.assertEqual(p.get("nas"), Credentials("fb", "fbpw"))        # nothing stored yet
        p.remember("nas", Credentials("alice", "secret"))
        self.assertEqual(kr.sets, 1)
        self.assertEqual(p.get("nas"), Credentials("alice", "secret"))   # now from the keychain
        p.remember("nas", Credentials("alice", "secret"))
        self.assertEqual(kr.sets, 1)                                      # unchanged -> not rewritten
        p.invalidate("nas")
        self.assertEqual(kr.deletes, 1)
        self.assertEqual(p.get("nas"), Credentials("fb", "fbpw"))

    def test_keyring_errors_fall_back_quietly(self):
        class Broken:
            def get_password(self, *a):
                raise RuntimeError("locked")

            def set_password(self, *a):
                raise RuntimeError("locked")
        p = KeyringCredentials(StaticCredentials("fb", "pw"), keyring_module=Broken())
        self.assertEqual(p.get("h"), Credentials("fb", "pw"))
        p.remember("h", Credentials("a", "b"))   # must not raise


class BatchedPromptTests(unittest.TestCase):
    def run_threads(self, prompt, sources):
        results = {}

        def call(src):
            results[src] = prompt(src, OSError("x"))
        ts = [threading.Thread(target=call, args=(s,)) for s in sources]
        [t.start() for t in ts]
        [t.join(10) for t in ts]
        return results

    def test_many_failures_one_question(self):
        asked, order = [], []
        prompt = BatchedFailurePrompt(lambda srcs: asked.append(sorted(srcs)) or True, window=0.2,
                                      before=lambda: order.append("before"), after=lambda: order.append("after"))
        res = self.run_threads(prompt, ["a", "b", "c"])
        self.assertEqual(asked, [["a", "b", "c"]])
        self.assertEqual(res, {"a": True, "b": True, "c": True})
        self.assertEqual(order, ["before", "after"])

    def test_duplicates_are_listed_once(self):
        asked = []
        prompt = BatchedFailurePrompt(lambda srcs: asked.append(list(srcs)) or False, window=0.2)
        results = []
        ts = [threading.Thread(target=lambda: results.append(prompt("same", OSError()))) for _ in range(3)]
        [t.start() for t in ts]
        [t.join(10) for t in ts]
        self.assertEqual(asked, [["same"]])
        self.assertEqual(results, [False, False, False])

    def test_failing_ask_means_no_retry_but_still_releases_everyone(self):
        order = []

        def ask(_):
            raise RuntimeError("dialog crashed")
        prompt = BatchedFailurePrompt(ask, window=0.1, after=lambda: order.append("after"))
        res = self.run_threads(prompt, ["a", "b"])
        self.assertEqual(res, {"a": False, "b": False})
        self.assertEqual(order, ["after"])

    def test_later_failures_start_a_new_batch(self):
        asked = []
        prompt = BatchedFailurePrompt(lambda s: asked.append(list(s)) or True, window=0.05)
        prompt("a", OSError())
        prompt("b", OSError())
        self.assertEqual(asked, [["a"], ["b"]])


if __name__ == "__main__":
    unittest.main()
