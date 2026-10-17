from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from threading import Barrier, Lock
import unittest
from unittest.mock import DEFAULT, patch
from urllib.error import URLError

from crypto_research import core, mcp
from crypto_research.__main__ import parser


class MCPTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict("os.environ", {name: "" for name, *_ in core.API_SETTINGS})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = Path(core.init("Example", root=self.root / "artifacts/runs")["run_dir"])
        download = patch.object(core, "download", return_value=(
            b'{"revenue": 0.1}', "application/json", 200, "https://example.com/data"))
        self.download = download.start()
        self.addCleanup(download.stop)

    def call(self, *args, tool_name="research"):
        return mcp.dispatch(list(args), root=self.root, tool_name=tool_name)

    def value(self, result):
        self.assertEqual(len(result["content"]), 1)
        self.assertEqual(result["content"][0]["type"], "text")
        return json.loads(result["content"][0]["text"])

    def request(self, method, params=None, identifier=1):
        request = {"jsonrpc": "2.0", "id": identifier, "method": method}
        if params is not None:
            request["params"] = params
        return request

    def serve(self, messages):
        output = io.StringIO()
        source = io.StringIO("".join(json.dumps(message) + "\n" for message in messages))
        with patch.object(mcp.Path, "cwd", return_value=self.root):
            mcp.serve(source, output)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_tool_contract_and_actual_help(self):
        response = mcp.handle_request(self.request("tools/list"))
        self.assertEqual(set(response["result"]), {"tools"})
        tools = response["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["research", "audit"])
        self.assertEqual(tools[0]["inputSchema"], tools[1]["inputSchema"])
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["required"], ["args"])
            self.assertEqual(tool["inputSchema"]["properties"]["args"]["items"]["type"], "string")
            self.assertIn(parser().format_help(), tool["description"])
            self.assertIn("UNTRUSTED SOURCE LIMITATION", tool["description"])
            self.assertNotIn("outputSchema", tool)
        self.assertIn("ONLY status, inspect, check and help", tools[1]["description"])

    def test_initialize_negotiates_supported_versions_only(self):
        for version in (*mcp.PROTOCOL_VERSIONS, "unsupported"):
            with self.subTest(version=version):
                result = mcp.handle_request(self.request("initialize", {"protocolVersion": version}))["result"]
                self.assertEqual(result["protocolVersion"], version if version in mcp.PROTOCOL_VERSIONS else mcp.PROTOCOL_VERSIONS[-1])
                self.assertEqual(result["capabilities"], {"tools": {}})
                self.assertEqual(result["serverInfo"]["version"], "0.1.0")
        self.assertEqual(mcp.handle_request(self.request("initialize"))["error"]["code"], -32602)

    def test_serve_protocol_and_notifications(self):
        messages = [self.request("initialize", {"protocolVersion": "2024-11-05"}),
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}},
                    self.request("ping", identifier="ping"),
                    self.request("tools/list", identifier=2),
                    self.request("tools/call", {"name": "research", "arguments": {"args": ["status", str(self.run)]}}, 3)]
        responses = {response["id"]: response for response in self.serve(messages)}
        self.assertEqual(set(responses), {1, "ping", 2, 3})
        self.assertEqual(responses["ping"]["result"], {})
        self.assertFalse(responses[3]["result"]["isError"])
        self.assertEqual(self.value(responses[3]["result"])["run_dir"], str(self.run))

    def test_notifications_never_run_commands(self):
        notification = {"jsonrpc": "2.0", "method": "tools/call", "params": {
            "name": "research", "arguments": {"args": ["init", "bad"]}}}
        with patch.object(core, "init") as init:
            self.assertEqual(self.serve([notification]), [])
            init.assert_not_called()

    def test_invalid_requests_ids_and_methods(self):
        for value in (None, [], "text", 1, {}, {"jsonrpc": "1.0", "method": "ping", "id": 1}):
            with self.subTest(value=value):
                self.assertEqual(mcp.handle_request(value)["error"]["code"], -32600)
        for identifier in (None, True, False, [], {}, 1.5):
            with self.subTest(identifier=identifier):
                error = mcp.handle_request(self.request("ping", identifier=identifier))
                self.assertEqual(error["error"]["code"], -32600)
                self.assertIsNone(error["id"])
        error = mcp.handle_request(self.request("shell", identifier="valid"))
        self.assertEqual(error["id"], "valid")
        self.assertEqual(error["error"]["code"], -32601)
        self.assertEqual(mcp.handle_request(self.request("ping", []))["error"]["code"], -32602)

    def test_tools_call_invalid_params(self):
        for params in ({}, {"name": "unknown"}, {"name": []}, {"name": "research", "arguments": []},
                       {"name": "research", "arguments": {"args": [], "root": "/tmp"}},
                       {"name": "audit", "arguments": {"args": [], "root": "/tmp"}}):
            with self.subTest(params=params):
                self.assertEqual(mcp.handle_request(self.request("tools/call", params))["error"]["code"], -32602)

    def test_audit_allowed_commands_do_not_change_saved_evidence(self):
        record = self.value(self.call("fetch", str(self.run), "https://example.com/data"))
        before = {path.relative_to(self.run): path.read_bytes() for path in self.run.rglob("*") if path.is_file()}
        status = self.call("status", str(self.run), tool_name="audit")
        self.assertFalse(status["isError"])
        self.assertEqual(self.value(status)["evidence_count"], 1)
        inspected = self.call("inspect", str(self.run), record["id"], "/revenue", tool_name="audit")
        self.assertFalse(inspected["isError"])
        self.assertEqual(self.value(inspected)["untrusted_preview"], "0.1")
        checked = self.call("check", str(self.run), tool_name="audit")
        self.assertFalse(checked["isError"])
        self.assertTrue(self.value(checked)["warnings"])
        final = self.call("check", str(self.run), "--final", tool_name="audit")
        self.assertTrue(final["isError"])
        self.assertIn("errors", self.value(final))
        after = {path.relative_to(self.run): path.read_bytes() for path in self.run.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.download.assert_called_once()

    def test_audit_rejects_all_network_and_mutation_commands_before_core(self):
        run = str(self.run)
        cases = {
            "preflight": ["preflight"],
            "init": ["init", "Project"],
            "fetch": ["fetch", run, "https://example.com/?api_key=SUPERSECRET"],
            "discover": ["discover", run, "Project"],
            "search": ["search", run, "Project announcements"],
            "protocol": ["protocol", run, "example"],
            "market": ["market", run, "example"],
            "fees": ["fees", run, "example"],
            "rpc": ["rpc", run, "eth_chainId"],
            "observe": ["observe", run, "ev_x", "/revenue", "--label", "Revenue", "--unit", "USD"],
            "calculate": ["calculate", run, "x * 2", "--inputs", '{"x":"obs_x"}', "--label", "Double", "--unit", "USD"],
            "identity": ["identity", run, "--name", "Project", "--official-url", "https://example.com", "--evidence", "ev_x"],
            "publish": ["publish", run],
        }
        self.assertEqual(set(cases), mcp.COMMANDS - mcp.TOOL_COMMANDS["audit"])
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with patch.multiple(core, **{command: DEFAULT for command in cases}) as functions:
            for command, args in cases.items():
                for invocation in (args, [command], [command, "--help"]):
                    with self.subTest(command=command, invocation=invocation):
                        request = self.request("tools/call", {"name": "audit", "arguments": {"args": invocation}})
                        result = mcp.handle_request(request, root=self.root)["result"]
                        self.assertTrue(result["isError"])
                        self.assertIn("Audit permits only", self.value(result)["error"])
                        self.assertNotIn("SUPERSECRET", json.dumps(result))
            for function in functions.values():
                function.assert_not_called()
        self.download.assert_not_called()
        after = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_preflight_tool_checks_all_settings_without_run_files_or_budget_use(self):
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with patch.dict("os.environ", {"DUNE_API_KEY": "dune-canary"}), \
                patch.object(core.socket, "getaddrinfo") as dns:
            result = self.call("preflight")
            empty_root_result = mcp.dispatch(["preflight"], root=self.root / "no-runs")
        self.assertFalse(result["isError"])
        self.assertFalse(empty_root_result["isError"])
        self.assertNotIn("dune-canary", json.dumps(result))
        settings = {item["environment"]: item for item in self.value(result)["settings"]}
        self.assertTrue(settings["DUNE_API_KEY"]["configured"])
        self.assertFalse(settings["DUNE_API_KEY"]["usable_by_local_adapter"])
        after = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        dns.assert_not_called()
        self.download.assert_not_called()

    def test_audit_can_inspect_failed_metadata_without_collecting(self):
        self.download.side_effect = URLError("SUPERSECRET")
        failed = self.value(self.call("fetch", str(self.run), "https://example.com/?api_key=SUPERSECRET"))
        calls = self.download.call_count
        result = self.call("inspect", str(self.run), failed["id"], tool_name="audit")
        self.assertFalse(result["isError"])
        self.assertEqual(self.value(result)["status"], "error")
        self.assertNotIn("SUPERSECRET", json.dumps(result))
        self.assertEqual(self.download.call_count, calls)

    def test_init_tool_returns_saved_preflight_without_secrets(self):
        with patch.dict("os.environ", {"EXA_API_KEY": "exa-canary"}):
            result = self.call("init", "New example")
        self.assertFalse(result["isError"])
        value = self.value(result)
        self.assertEqual(value["preflight"], core.load(Path(value["run_dir"]) / "run.json")["preflight"])
        self.assertNotIn("exa-canary", json.dumps(result))
        self.assertEqual(core.status(value["run_dir"])["budget"]["requests_started"], 0)
        self.download.assert_not_called()

    def test_audit_help_has_no_execution_or_stdout_side_effects(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), patch.object(core, "status") as status:
            for args in (("--help",), ("-h",), ("status", "--help"), ("inspect", "--help"), ("check", "--help")):
                with self.subTest(args=args):
                    result = self.call(*args, tool_name="audit")
                    self.assertFalse(result["isError"])
                    if args[0] in mcp.COMMANDS:
                        self.assertIn(" " + args[0] + " ", self.value(result)["help"])
                    else:
                        self.assertEqual(self.value(result)["help"], parser().format_help())
            status.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")
        self.download.assert_not_called()

    def test_audit_retains_bounds_root_and_symlink_restrictions(self):
        outside = Path(core.init("Outside", root=self.root / "outside")["run_dir"])
        invalid = [["status", str(outside)], ["status", str(self.run), "--root", str(self.root)],
                   ["status", str(self.run), "x" * 2049], ["--help"] * 65]
        with patch.object(core, "status") as status:
            for args in invalid:
                self.assertTrue(mcp.dispatch(args, root=self.root, tool_name="audit")["isError"])
            alias = self.run.parent / "alias"
            alias.symlink_to(self.run, target_is_directory=True)
            self.assertTrue(self.call("status", str(alias), tool_name="audit")["isError"])
            status.assert_not_called()

    def test_serve_routes_audit_and_research_with_separate_permissions(self):
        messages = [self.request("tools/call", {"name": name, "arguments": {"args": args}}, index)
                    for index, (name, args) in enumerate((
                        ("audit", ["fetch", str(self.run), "https://example.com/data"]),
                        ("research", ["fetch", str(self.run), "https://example.com/data"]),
                        ("audit", ["status", str(self.run)]),
                        ("audit", ["--help"])))]
        responses = {response["id"]: response["result"] for response in self.serve(messages)}
        self.assertTrue(responses[0]["isError"])
        self.assertTrue(all(not responses[index]["isError"] for index in (1, 2, 3)))
        self.download.assert_called_once()

    def test_parse_errors_large_lines_and_recovery(self):
        output = io.StringIO()
        source = io.StringIO("{broken-secret\n[]\nNaN\n" + "x" * (mcp.MAX_LINE * 3) + "\n"
                             + json.dumps(self.request("ping")) + "\n")
        mcp.serve(source, output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([response["error"]["code"] for response in responses[:-1]],
                         [-32700, -32600, -32700, -32600])
        self.assertEqual(responses[-1]["result"], {})
        self.assertNotIn("secret", output.getvalue())

    def test_line_cap_counts_utf8_bytes(self):
        output = io.StringIO()
        mcp.serve(io.StringIO('"' + "\u00e9" * (mcp.MAX_LINE // 2) + '"\n'
                             + json.dumps(self.request("ping")) + "\n"), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertEqual(responses[1]["result"], {})

    def test_init_uses_only_internal_run_root(self):
        result = self.call("init", "Another", "--max-requests", "2")
        self.assertFalse(result["isError"])
        directory = Path(self.value(result)["run_dir"])
        self.assertEqual(directory.parent, self.root / "artifacts/runs")
        self.assertTrue((directory / "run.json").is_file())
        for args in (("init", "bad", "--root", str(self.root)),
                     ("init", "bad", "--root=" + str(self.root)),
                     ("init", "bad", "--ro", str(self.root))):
            with self.subTest(args=args), patch.object(core, "init") as init, redirect_stderr(io.StringIO()):
                self.assertTrue(self.call(*args)["isError"])
                init.assert_not_called()

    def test_relative_run_path_resolves_against_server_root(self):
        result = self.call("status", str(self.run.relative_to(self.root)))
        self.assertFalse(result["isError"])
        self.assertEqual(self.value(result)["run_dir"], str(self.run))

    def test_run_containment_and_existing_manifest_required(self):
        outside = Path(core.init("Outside", root=self.root / "outside")["run_dir"])
        candidates = [outside, self.root / "artifacts/runs", self.root / "artifacts/runs/missing",
                      "artifacts/runs/../../outside/" + outside.name,
                      self.root / "artifacts/runs-sibling" / outside.name]
        for candidate in candidates:
            with self.subTest(candidate=candidate), patch.object(core, "status") as status:
                self.assertTrue(self.call("status", str(candidate))["isError"])
                status.assert_not_called()

    def test_symlink_run_rejected_even_if_target_inside_base(self):
        alias = self.run.parent / "alias"
        alias.symlink_to(self.run, target_is_directory=True)
        self.assertTrue(self.call("status", str(alias))["isError"])

    def test_symlinked_run_contents_never_written(self):
        external = self.root / "external"
        external.write_text("unchanged")
        (self.run / ".lock").symlink_to(external)
        with patch.object(core, "status") as status:
            self.assertTrue(self.call("status", str(self.run))["isError"])
            status.assert_not_called()
        self.assertEqual(external.read_text(), "unchanged")

    def test_symlinked_evidence_directory_rejected(self):
        external = self.root / "external-evidence"
        external.mkdir()
        (self.run / "redirect").symlink_to(external, target_is_directory=True)
        self.assertTrue(self.call("fetch", str(self.run), "https://example.com/data")["isError"])
        self.download.assert_not_called()
        self.assertEqual(list(external.iterdir()), [])

    def test_symlinked_init_parent_rejected(self):
        other_root = self.root / "other"
        other_root.mkdir()
        (other_root / "artifacts").symlink_to(self.root / "artifacts", target_is_directory=True)
        with patch.object(core, "init") as init:
            self.assertTrue(mcp.dispatch(["init", "bad"], root=other_root)["isError"])
            init.assert_not_called()

    def test_direct_fetch_inspect_observe_calculate_identity(self):
        record_result = self.call("fetch", str(self.run), "https://example.com/data", "--kind", "official")
        self.assertFalse(record_result["isError"])
        record = self.value(record_result)
        inspected = self.call("inspect", str(self.run), record["id"], "/revenue")
        self.assertFalse(inspected["isError"])
        self.assertEqual(self.value(inspected)["untrusted_preview"], "0.1")
        observed = self.call("observe", str(self.run), record["id"], "/revenue", "--label", "Revenue", "--unit", "USD")
        self.assertFalse(observed["isError"])
        metric = self.value(observed)
        self.assertEqual(metric["json_pointer"], "/revenue")
        calculated = self.call("calculate", str(self.run), "x * 2", "--inputs", json.dumps({"x": metric["id"]}),
                               "--label", "Double", "--unit", "USD")
        self.assertFalse(calculated["isError"])
        self.assertEqual(self.value(calculated)["value"], "0.2")
        identity = self.call("identity", str(self.run), "--name", "Example", "--official-url", "https://example.com",
                             "--chain", "Arbitrum", "--chain", "Ethereum", "--evidence", record["id"])
        self.assertFalse(identity["isError"])
        self.assertEqual(self.value(identity)["chains"], ["Arbitrum", "Ethereum"])
        self.assertEqual(self.value(identity)["evidence_ids"], [record["id"]])
        self.download.assert_called_once()

    def test_provider_commands_and_rpc_use_core(self):
        for command in ("protocol", "market", "fees"):
            with self.subTest(command=command):
                self.assertFalse(self.call(command, str(self.run), "example")["isError"])
        discovered = self.call("discover", str(self.run), "Example")
        self.assertFalse(discovered["isError"])
        self.assertEqual(set(self.value(discovered)), {"coingecko", "defillama"})
        self.download.return_value = (b'{"result":"0x1"}', "application/json", 200, "https://example.com/rpc")
        with patch.dict("os.environ", {"CR_RPC_URL": "https://example.com/rpc"}):
            self.assertFalse(self.call("rpc", str(self.run), "eth_chainId", "--params", "[]")["isError"])

    def test_check_warnings_vs_failures(self):
        warning = self.call("check", str(self.run))
        self.assertFalse(warning["isError"])
        self.assertTrue(self.value(warning)["warnings"])
        final = self.call("check", str(self.run), "--final")
        self.assertTrue(final["isError"])
        self.assertFalse(self.value(final)["ok"])

    def test_failed_fetch_and_discover_mark_is_error_without_secrets(self):
        self.download.side_effect = URLError("https://example.com/?key=SUPERSECRET")
        for args in (("fetch", str(self.run), "https://example.com/?api_key=SUPERSECRET"),
                     ("discover", str(self.run), "Example")):
            with self.subTest(args=args):
                result = self.call(*args)
                self.assertTrue(result["isError"])
                self.assertNotIn("SUPERSECRET", json.dumps(result))

    def test_discover_any_failed_status_marks_error(self):
        with patch.object(core, "discover", return_value={"one": {"status": "ok"}, "two": {"status": "unavailable"}}):
            self.assertTrue(self.call("discover", str(self.run), "Example")["isError"])

    def test_parser_errors_and_help_never_contaminate_stdout(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            for args in (("shell", "SUPERSECRET"), ("init", "Project", "--minutes", "SUPERSECRET"),
                         ("rpc", str(self.run), "eth_chainId", "--params", "SUPERSECRET"),
                         ("init", "Project", "--hel")):
                result = self.call(*args)
                self.assertTrue(result["isError"])
                self.assertNotIn("SUPERSECRET", json.dumps(result))
            for args in (("--help",), ("init", "--help"), ("-h",)):
                result = self.call(*args)
                self.assertFalse(result["isError"])
                if args[0] == "init":
                    self.assertIn("--max-requests", self.value(result)["help"])
                else:
                    self.assertEqual(self.value(result)["help"], parser().format_help())
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("SUPERSECRET", stderr.getvalue())
        self.assertIn("Invalid command arguments", stderr.getvalue())

    def test_argument_bounds_before_core_dispatch(self):
        invalid = [None, "init", [1], ["x"] * 65, ["x" * 2049], ["x" * 2048] * 9,
                   ["\u00e9" * 2048] * 5, ["\ud800"]]
        with patch.object(core, "init") as init:
            for args in invalid:
                with self.subTest(args_type=type(args)):
                    self.assertTrue(mcp.dispatch(args, root=self.root)["isError"])
            init.assert_not_called()

    def test_argument_bounds_accept_exact_limits(self):
        args = ["--help"] + ["x" * 2048] * 7 + ["x" * 2042] + [""] * 55
        self.assertEqual(len(args), 64)
        self.assertEqual(sum(len(arg.encode("utf-8")) for arg in args), 16 * 1024)
        self.assertFalse(mcp.dispatch(args, root=self.root)["isError"])
        args[8] += "x"
        self.assertTrue(mcp.dispatch(args, root=self.root)["isError"])

    def test_subcommand_help_exposes_actual_flags_without_execution(self):
        for command, flag in (("observe", "--quote"), ("rpc", "--params"), ("check", "--final")):
            with self.subTest(command=command):
                result = self.call(command, "--help")
                self.assertFalse(result["isError"])
                self.assertIn(flag, self.value(result)["help"])
        self.download.assert_not_called()

    def test_serve_parser_help_and_errors_do_not_leak_stdout(self):
        messages = [self.request("tools/call", {"name": "research", "arguments": {"args": args}}, index)
                    for index, args in enumerate((["--help"], ["init", "--minutes", "SUPERSECRET"]))]
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            responses = {response["id"]: response for response in self.serve(messages)}
        self.assertFalse(responses[0]["result"]["isError"])
        self.assertTrue(responses[1]["result"]["isError"])
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("SUPERSECRET", stderr.getvalue() + json.dumps(responses))

    def test_serve_internal_errors_do_not_echo_exceptions(self):
        message = self.request("tools/call", {"name": "research", "arguments": {"args": ["status", str(self.run)]}})
        with patch.object(mcp, "dispatch", side_effect=RuntimeError("SUPERSECRET URL")):
            response = self.serve([message])[0]
        self.assertEqual(response["error"]["code"], -32603)
        self.assertNotIn("SUPERSECRET", json.dumps(response))
        with patch.object(mcp, "tool", side_effect=RuntimeError("SUPERSECRET URL")):
            response = self.serve([self.request("tools/list")])[0]
        self.assertEqual(response["error"]["code"], -32603)
        self.assertNotIn("SUPERSECRET", json.dumps(response))

    def test_unexpected_exceptions_are_sanitized(self):
        for exception in (RuntimeError("SUPERSECRET URL"), OSError("SUPERSECRET path"), ValueError("SUPERSECRET value")):
            with self.subTest(exception=exception), patch.object(core, "status", side_effect=exception):
                result = self.call("status", str(self.run))
                self.assertTrue(result["isError"])
                self.assertEqual(self.value(result)["error"], "Research operation failed")
                self.assertNotIn("SUPERSECRET", json.dumps(result))

    def test_domain_errors_remain_actionable(self):
        result = self.call("protocol", str(self.run), "NOT-A-CANONICAL-ID")
        self.assertTrue(result["isError"])
        self.assertIn("canonical lowercase source ID", self.value(result)["error"])

    def test_parallel_dispatch_respects_shared_budget(self):
        manifest = core.load(self.run / "run.json")
        manifest["budget"]["max_requests"] = 2
        core.save(self.run / "run.json", manifest)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda index: self.call("fetch", str(self.run), "https://example.com/data?i=" + str(index)), range(12)))
        self.assertEqual(sum(not result["isError"] for result in results), 2)
        self.assertEqual(self.download.call_count, 2)
        self.assertEqual(core.status(self.run)["budget"]["requests_started"], 2)
        self.assertEqual(core.status(self.run)["evidence_count"], 2)

    def test_serve_parallel_workers_and_atomic_output(self):
        barrier = Barrier(6)
        lock = Lock()
        active = 0
        peak = 0
        threads = set()

        def status(run):
            nonlocal active, peak
            import threading
            with lock:
                active += 1
                peak = max(peak, active)
                threads.add(threading.get_ident())
            barrier.wait(timeout=5)
            with lock:
                active -= 1
            return {"ok": True, "padding": "x" * 4096}

        messages = [self.request("tools/call", {"name": "research", "arguments": {
            "args": ["status", str(self.run)]}}, index) for index in range(12)]
        with patch.object(core, "status", side_effect=status):
            responses = self.serve(messages)
        self.assertEqual({response["id"] for response in responses}, set(range(12)))
        self.assertTrue(all(not response["result"]["isError"] for response in responses))
        self.assertEqual(peak, 6)
        self.assertEqual(len(threads), 6)


class ProjectConfigTests(unittest.TestCase):
    root = Path(__file__).resolve().parents[1]

    def test_build_backend_supports_spdx_license_metadata(self):
        import tomllib

        config = tomllib.loads((self.root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["build-system"]["requires"], ["setuptools>=77"])
        self.assertEqual(config["project"]["license"], "MIT")
        self.assertEqual(config["project"]["license-files"], ["LICENSE"])
        self.assertEqual(config["project"]["dependencies"], [])

    def test_reports_are_ignored_and_full_research_publishes_only_after_final_gate(self):
        patterns = (self.root / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("/reports/", patterns)
        self.assertIn("artifacts/", patterns)
        command = (self.root / ".kilo/command/research.md").read_text(encoding="utf-8")
        self.assertIn('["publish","RUN"]', command)
        self.assertIn("After a passing final gate", command)

    def test_deep_research_default_keeps_research_and_secret_guards(self):
        config = json.loads((self.root / "kilo.json").read_text(encoding="utf-8"))
        self.assertEqual(config["default_agent"], "crypto-research")
        permissions = config["permission"]
        self.assertEqual(permissions["*"], "ask")
        for tool in ("edit", "task", "webfetch", "websearch", "external_directory"):
            self.assertEqual(permissions[tool], "deny")
        for tool in ("bash", "coingecko_*", "etherscan_*"):
            self.assertEqual(permissions[tool], "ask")
        for pattern in (".env", ".env.*", "**/.env", "**/.env.*", "**/auth.json", "**/*.pem", "**/*.key"):
            self.assertEqual(permissions["read"][pattern], "deny")

    def test_code_agent_allows_development_but_not_secrets_or_research_artifacts(self):
        agent = (self.root / ".kilo/agent/crypto-code.md").read_text(encoding="utf-8")
        permissions = agent.split("---", 2)[1]
        self.assertIn('  edit:\n    "*": allow\n', permissions)
        for tool in ("bash", "webfetch", "websearch", "external_directory"):
            self.assertIn("  " + tool + ": allow\n", permissions)
        for tool in ("coingecko_*", "etherscan_*"):
            self.assertIn("  " + tool + ": ask\n", permissions)
        self.assertIn("  task: deny\n", permissions)
        for pattern in (".env", ".env.*", "**/.env", "**/.env.*", "artifacts/**"):
            self.assertIn('    "' + pattern + '": deny\n', permissions)

    def test_env_example_contains_only_empty_unique_values_and_marks_future_paid_keys(self):
        template = (self.root / "env.example").read_text(encoding="utf-8")
        entries = [line for line in template.splitlines() if line and not line.startswith("#")]
        names = []
        for entry in entries:
            name, value = entry.split("=", 1)
            self.assertTrue(name.isidentifier())
            self.assertEqual(value, "")
            names.append(name)
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue({"COINGECKO_DEMO_API_KEY", "CR_RPC_URL"}.issubset(names))
        self.assertIn("остальные ключи пока только проверяются", template)
        self.assertIn("автозагрузки нет", template)
        paid_section = template.split("# Платные планы и наборы данных", 1)[1]
        for key in ("CRYPTORANK_API_KEY", "GOLDRUSH_API_KEY", "COINGECKO_PRO_API_KEY"):
            self.assertIn(key + "=\n", paid_section)

    def test_preflight_registry_covers_every_template_variable_in_order(self):
        template = (self.root / "env.example").read_text(encoding="utf-8")
        names = [line.split("=", 1)[0] for line in template.splitlines() if line and not line.startswith("#")]
        self.assertEqual(names, [name for name, *_ in core.API_SETTINGS])
        self.assertEqual({name for name, _, _, supported in core.API_SETTINGS if supported},
                         {"COINGECKO_DEMO_API_KEY", "CR_RPC_URL"})

    def test_env_example_orders_free_basic_optional_and_paid_sections(self):
        template = (self.root / "env.example").read_text(encoding="utf-8")
        sections = (
            ("# Бесплатные", ("COINGECKO_DEMO_API_KEY", "CONTEXT7_API_KEY")),
            ("# Базовые", ("CR_RPC_URL",)),
            ("# Опциональные", ("EXA_API_KEY", "TAVILY_API_KEY", "FIRECRAWL_API_KEY",
                                         "DUNE_API_KEY", "THEGRAPH_API_KEY", "ALCHEMY_API_KEY",
                                         "ETHERSCAN_API_KEY", "MOBULA_API_KEY")),
            ("# Платные планы и наборы данных", ("CRYPTORANK_API_KEY", "GOLDRUSH_API_KEY", "COINGECKO_PRO_API_KEY")),
        )
        positions = [template.index(heading) for heading, _ in sections]
        self.assertEqual(positions, sorted(positions))
        boundaries = positions + [len(template)]
        for index, (_, keys) in enumerate(sections):
            section = template[boundaries[index]:boundaries[index + 1]]
            for key in keys:
                with self.subTest(key=key):
                    self.assertIn("\n" + key + "=\n", section)


if __name__ == "__main__":
    unittest.main()
