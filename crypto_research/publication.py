"""Readable Markdown exports from final-checked research snapshots."""

from datetime import datetime, timezone
import hashlib
import html
import os
from pathlib import Path
import re
import stat
import tempfile
import unicodedata
from urllib.parse import quote, urlsplit

from . import core


ARTIFACT_ID = re.compile(r"\b(?:ev|obs|calc)_[a-f0-9]{12}\b")
CLAIM_REFS = re.compile(r"\[((?:claim-[\w-]+|[CS]\d+)(?:\s*(?:,|[–-])\s*(?:claim-[\w-]+|[CS]\d+))*)\]")
INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)(.*?)\1(?!`)", re.DOTALL)
ID_LIST = r"`?(?:ev|obs|calc)_[a-f0-9]{12}`?(?:(?:\s*[,;/+]\s*|\s+(?:и|and)\s+)`?(?:ev|obs|calc)_[a-f0-9]{12}`?)*"
INLINE_REF = re.compile(CLAIM_REFS.pattern + "|" + ID_LIST)
LINEAGE = re.compile(r"\b(Доли|Требования|Метрики|Метрика|Входы|Metric IDs|Metrics|Inputs)\s*:\s*" + ID_LIST + r"(?:\s*(\([^()\n]*\)))?", re.IGNORECASE)
ISO_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\b")
SOURCE_HEADINGS = {"источники", "основные источники", "sources", "references"}
SOURCE_LINK = re.compile(r"(?<!!)\[([^\]\n]+)\]\((https?://(?:[^()\s]|\([^()\s]*\))+)\)")


def readable_date(value, *, seconds=False):
    try:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if "T" not in str(value):
        return date.strftime("%d.%m.%Y")
    if date.tzinfo is None:
        return str(value)
    return date.astimezone(timezone.utc).strftime("%d.%m.%Y, %H:%M:%S UTC" if seconds else "%d.%m.%Y, %H:%M UTC")


def fence_state(line, current):
    match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
    if not match:
        return current
    marker, suffix = match.groups()
    if current is None:
        return marker
    if marker[0] == current[0] and len(marker) >= len(current) and not suffix.strip():
        return None
    return current


def plain(value):
    text = html.escape(" ".join(str(value).split()), quote=False)
    return re.sub(r"([\\`*\[\]|])", r"\\\1", text)


def reference_ids(match):
    if not match[1]:
        return ARTIFACT_ID.findall(match[0])
    identifiers = []
    for part in re.split(r"\s*,\s*", match[1]):
        span = re.fullmatch(r"([CS])(\d+)\s*[–-]\s*\1(\d+)", part)
        if span:
            start, end = int(span[2]), int(span[3])
            if not start <= end <= start + 100:
                raise core.ResearchError("Invalid or excessive report reference range")
            identifiers.extend(span[1] + str(index) for index in range(start, end + 1))
        else:
            identifiers.append(part)
    return identifiers


def project_slug(name):
    value = unicodedata.normalize("NFKC", name).casefold()
    return re.sub(r"[^\w-]+", "-", value).strip("-_")[:80].rstrip("-_") or "research"


def source_url(record):
    url = core.safe_url(record["url"], rpc=record["kind"] == "rpc")
    parts = urlsplit(url)
    return quote(url, safe="/:?=&%+@~") if parts.scheme == "https" and parts.hostname else None


def source_link(record):
    url = source_url(record)
    parts = urlsplit(record["url"])
    label = plain(record.get("label") or parts.hostname or "Источник")
    return "[" + label + "](" + url + ")" if url else label


class Renderer:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.records = {item["id"]: item for item in snapshot["evidence"]}
        self.metrics = {item["id"]: item for item in snapshot["metrics"]}
        self.claims = {item["id"]: item for item in snapshot["claims"]}
        self.sources = []
        self.used_metrics = set()
        self.source_aliases = {match[1]: ARTIFACT_ID.findall(match[2]) for match in
                               re.finditer(r"(?m)^[-*]\s+(S\d+)\s+[—:-]?\s*(.*)$", snapshot["report"])}
        self.source_notes = {}
        self.loose_source_notes = []

    def source_ids(self, identifiers):
        sources = []
        for identifier in identifiers:
            if identifier in self.records:
                source_ids = [identifier]
            elif identifier in self.source_aliases:
                source_ids = self.source_ids(self.source_aliases[identifier])
            elif identifier in self.metrics:
                self.track_metric(identifier)
                source_ids = self.metrics[identifier]["evidence_ids"]
            elif identifier in self.claims:
                claim = self.claims[identifier]
                source_ids = list(claim["evidence_ids"])
                for metric_id in claim["metric_ids"]:
                    self.track_metric(metric_id)
                    source_ids.extend(self.metrics[metric_id]["evidence_ids"])
            else:
                raise core.ResearchError("Report references an unknown claim, source or metric")
            for source_id in source_ids:
                if source_id not in sources:
                    sources.append(source_id)
        return sources

    def track_metric(self, identifier):
        if identifier in self.used_metrics:
            return
        self.used_metrics.add(identifier)
        for dependency in self.metrics[identifier].get("inputs", {}).values():
            self.track_metric(dependency)

    def linked_sources(self, url):
        return [identifier for identifier, record in self.records.items()
                if url in {record["url"], record.get("final_url"), source_url(record)}]

    def references(self, identifiers):
        """Resolve and retain provenance without putting citations in the prose."""
        for source_id in self.source_ids(identifiers):
            if source_id not in self.sources:
                self.sources.append(source_id)
        return ""

    def prose(self, text, *, table=False):
        has_claims = bool(CLAIM_REFS.search(text))
        protected = {}
        prefix = "\ufff0code"
        while prefix in text:
            prefix += "_"

        def protect(match):
            if ARTIFACT_ID.fullmatch(match[2]):
                return match[2]
            key = prefix + str(len(protected)) + "\ufff1"
            protected[key] = match[0]
            return key

        text = INLINE_CODE.sub(protect, text)

        def source_label(match):
            identifiers = self.linked_sources(match[2])
            if not identifiers:
                if match[1].isdigit():
                    raise core.ResearchError("Numbered source link is not backed by saved evidence")
                return match[0]
            self.references(identifiers)
            return "" if match[1].isdigit() else match[1]

        text = SOURCE_LINK.sub(source_label, text)

        def protect_url(match):
            key = prefix + str(len(protected)) + "\ufff1"
            protected[key] = match[0]
            return key

        text = re.sub(r"https?://[^\s<>`]+", protect_url, text)
        for match in INLINE_REF.finditer(text):
            self.source_ids(reference_ids(match))
        if not table:
            text = LINEAGE.sub(lambda match: match[1] + ": " + match[2][1:-1] if match[2] else "", text)

        def collect(match):
            return self.references(reference_ids(match))

        operational = text.startswith(("Dedicated apply_patch", "Ошибки инструментов", "К журналу добавлены",
                                       "Выполнены четыре native", "В audit trail", "Один старый MCP", "Старый MCP-validator"))
        if operational and not table and not has_claims:
            return ""
        text = re.sub(r"\s+использует коэффициент\s+" + ID_LIST, "", text)
        text = INLINE_REF.sub(collect, text)
        text = re.sub(r"\s+[—–-]\s*(?=[.;,]|$)", "", text)
        text = re.sub(r"\s+(?:и|and)\s*(?=[.;,]|$)", "", text)
        text = re.sub(r"\s+([.;,])", r"\1", text)
        text = re.sub(r"\s*[,;:]\s*(?=[.;,]|$)", "", text)
        text = re.sub(r"(?m)^\s*[.;]\s*", "", text)
        text = re.sub(r" {2,}", " ", text)
        text = ISO_TIME.sub(lambda match: readable_date(match[0], seconds=True), text)
        text = html.escape(text, quote=False)
        for key, value in protected.items():
            text = text.replace(key, value)
        return text.strip()

    def table(self, rows):
        cells = []
        for row in rows:
            split = re.split(r"(?<!\\)\|", row)[1:]
            if not split[-1].strip():
                split.pop()
            cells.append(split)
        rendered = [[self.prose(cell.strip(), table=True) for cell in row] for row in cells]
        # Drop only dedicated lineage columns, never a column containing actual analysis.
        hidden = {index for index, header in enumerate(cells[0])
                  if header.strip().casefold() in {"evidence / metric", "metric id", "метрики", "источники"}
                  and all(index < len(row) and not row[index].strip() for row in rendered[2:])}
        if hidden and len(hidden) == len(cells[0]):
            return []
        return ["| " + " | ".join(cell for index, cell in enumerate(row) if index not in hidden) + " |"
                for row in rendered]

    def text(self, text):
        lines, paragraph, table, fenced = [], [], [], None

        def flush():
            if paragraph:
                rendered = self.prose("\n".join(paragraph))
                if rendered:
                    lines.append(rendered)
                paragraph.clear()

        def flush_table():
            if table:
                lines.extend(self.table(table))
                table.clear()

        for line in text.splitlines():
            if not line.startswith("|"):
                flush_table()
            next_fence = fence_state(line, fenced)
            if fenced or next_fence:
                flush()
                lines.append(line)
                fenced = next_fence
            elif not line.strip():
                flush()
                if lines and lines[-1] != "":
                    lines.append("")
            elif line.strip() == core.VERIFICATION_NOTICE:
                flush()
            elif line.startswith("|"):
                flush()
                table.append(line)
            else:
                block = re.match(r"^(\s*(?:[-*+] |\d+[.)] |#{1,6} |> ?))(.*)$", line)
                if block:
                    flush()
                    rendered = self.prose(block[2])
                    if rendered:
                        lines.append(block[1] + rendered)
                else:
                    paragraph.append(line)
        flush()
        flush_table()
        return "\n".join(lines).strip()

    def split_sections(self):
        title, preamble, sections = None, [], []
        current, fenced = preamble, None
        for line in self.snapshot["report"].splitlines():
            previous = fenced
            fenced = fence_state(line, fenced)
            if previous or fenced:
                current.append(line)
                continue
            if not fenced and title is None and line.startswith("# "):
                title = line[2:]
            elif not fenced and line.startswith("## "):
                current = []
                sections.append((line[3:], current))
            else:
                current.append(line)
        if not sections:
            sections = [("Исследование", preamble)]
            preamble = []
        if fenced:
            raise core.ResearchError("Report has an unclosed code block")
        return title or self.snapshot["manifest"]["identity"]["name"], preamble, sections

    def collect_source_notes(self, sections):
        for _, body in sections:
            for line in body:
                if re.match(r"^[-*]\s+S\d+\s", line):
                    continue
                links = list(SOURCE_LINK.finditer(line))
                if links and not SOURCE_LINK.sub("", line).strip(" \t-*+,;.") and all(self.linked_sources(link[2]) for link in links):
                    for link in links:
                        self.references(self.linked_sources(link[2]))
                    continue
                for match in INLINE_REF.finditer(line):
                    self.references(reference_ids(match))
                identifiers = ARTIFACT_ID.findall(line)
                if len(identifiers) != 1 or identifiers[0] not in self.records or ";" not in line or not re.search(r"https?://", line):
                    self.loose_source_notes.append(line)
                    continue
                notes = []
                for part in line.split(";")[1:]:
                    part = part.strip(" .")
                    if re.match(r"(?:получено|retrieved|fetched|received|время загрузки)", part, re.IGNORECASE):
                        continue
                    if re.fullmatch(r"(?:дата|время|publication time|source time).*?(?:неизвест\w*|unknown|не установлен\w*)", part, re.IGNORECASE):
                        continue
                    if part:
                        notes.append(part)
                if notes:
                    self.source_notes.setdefault(identifiers[0], []).extend(notes)

    def separate_sources(self, sections):
        """Move bibliography subsections to the end, without interpreting code as headings."""
        content, notes = [], []
        for heading, body in sections:
            if heading.casefold() in SOURCE_HEADINGS:
                notes.append((heading, body))
                continue
            kept, extracted, source_level, fenced = [], [], None, None
            for line in body:
                previous = fenced
                fenced = fence_state(line, fenced)
                match = None if previous or fenced else re.match(r"^(#{3,6})\s+(.+?)\s*$", line)
                if match and source_level and len(match[1]) <= source_level:
                    source_level = None
                if match and match[2].casefold() in SOURCE_HEADINGS:
                    source_level = len(match[1])
                    continue
                (extracted if source_level else kept).append(line)
            content.append((heading, kept))
            if extracted:
                notes.append(("Источники", extracted))
        return content, notes

    def warnings(self, final_check):
        lines, unknown, unavailable = [], False, False
        for warning in final_check["warnings"]:
            if warning.startswith("Unavailable source: "):
                identifier = warning.partition(": ")[2]
                self.references([identifier])
                unavailable = True
            elif warning.startswith("Observation period unknown:"):
                unknown = True
            elif warning.startswith("Research coverage is "):
                lines.append("Целостность артефактов не означает полноту исследования; статус покрытия указан отдельно.")
            elif warning in {"No confirmed independent cross-model verification",
                             "Structural checks do not prove source truth, claim entailment, dimensional correctness or investment quality"}:
                continue
            else:
                lines.append(plain(warning))
        if unknown:
            lines.append("У части наблюдений нет собственной даты или периода. Время загрузки не подменяет время данных.")
        if unavailable:
            lines.append("Часть источников недоступна; они отмечены в списке источников и не подтверждают показатели проекта.")
        return lines

    def bibliography(self):
        lines = []
        for identifier in self.sources:
            record = self.records[identifier]
            periods = list(dict.fromkeys((metric["label"], metric["as_of"])
                                         for metric in self.snapshot["metrics"]
                                         if metric["id"] in self.used_metrics and metric["type"] == "observation"
                                         and identifier in metric["evidence_ids"] and metric.get("as_of")
                                         and not re.search(r"undated|unknown|неизвест|не установлен", metric["as_of"], re.IGNORECASE)))
            lines.append("- **" + source_link(record) + "**. Получено " + readable_date(record["retrieved_at"]) + ".")
            for label, period in periods:
                lines.append("  - Период показателя «" + plain(label) + "»: " + plain(readable_date(period, seconds=True)) + ".")
            for note in dict.fromkeys(self.source_notes.get(identifier, [])):
                lines.append("  - " + plain(ISO_TIME.sub(lambda match: readable_date(match[0], seconds=True), note)) + ".")
            if record["status"] != "ok":
                metadata = core.source_metadata(record)
                reason = "HTTP " + str(metadata["http_status"]) if metadata["http_status"] else "Ошибка получения данных"
                lines.append("  - **Источник недоступен: " + reason + ".** Не подтверждает показатели проекта.")
            lines.append("")
        return lines

    def render(self, final_check):
        title, preamble, sections = self.split_sections()
        sections, notes = self.separate_sources(sections)
        memo = [section for section in sections if section[0].casefold() in {"краткое заключение", "резюме", "краткий вывод", "короткий вывод"}]
        remaining = [section for section in sections if section not in memo]
        ordered = memo + remaining
        rendered = [(heading, self.text("\n".join(body))) for heading, body in ordered]
        self.collect_source_notes(notes)
        source_notes = self.text("\n".join(self.loose_source_notes))
        metadata = self.text(ISO_TIME.sub(lambda match: readable_date(match[0], seconds=True), "\n".join(preamble)))
        warnings = self.warnings(final_check)
        identity = self.snapshot["manifest"]["identity"]
        verification = self.snapshot["verification"]
        review = {"independent": "Заявлена независимая проверка; семейства моделей требуют сверки",
                  "same_model_or_unknown": "Ревью выполнено; независимость моделей не подтверждена",
                  "not_performed": "Ревью не выполнено"}[verification["status"]]
        token = {"unknown": "Не подтверждён", "none_verified": "Отсутствие подтверждено официально",
                 "confirmed": "Подтверждён по записанной идентичности"}[identity["token_status"]]
        lines = ["# " + plain(title), "", "**Исследование · " +
                 readable_date(datetime.fromisoformat(self.snapshot["manifest"]["created_at"]).date()) + "**", "",
                 "| Проект | Сеть | Статус токена |", "| --- | --- | --- |",
                 "| " + source_link({"url": identity["official_url"], "kind": "official", "label": identity["name"]}) +
                 " | " + plain(", ".join(identity["chains"]) or "Не установлена") +
                  " | " + token + " |", ""]
        quality = self.snapshot.get("quality", {"status": "unassessed"})
        label = {"adequate": "ключевые вопросы покрыты по заявленной карте и проверке достаточности",
                 "partial": "частичный ответ — решающие пробелы сохраняются",
                 "unassessed": "полнота ответа не оценена"}[quality["status"]]
        lines.extend(["> **Статус оценки:** " + label + ".", ""])
        # coverage.json is a machine-facing audit artifact, not an executive summary.
        for heading, body in rendered[:len(memo)]:
            lines.extend(["## " + plain(heading), "", body.strip(), ""])
        lines.extend(["## Содержание", ""])
        for heading, _ in rendered:
            anchor = re.sub(r"[^\w -]", "", heading.casefold()).replace(" ", "-")
            lines.append("- [" + plain(heading) + "](#" + quote(anchor) + ")")
        lines.extend(["- [Источники](#" + quote("источники") + ")", ""])
        for heading, body in rendered[len(memo):]:
            lines.extend(["## " + plain(heading), "", body.strip(), ""])
        lines.extend(["---", "", "## Даты и границы исследования", "", metadata, "",
                       "### Проверка публикации", "",
                       "- " + review + ".",
                       "- Структурная проверка перед публикацией: **пройдена**. Она не доказывает истинность источников или справедливость оценки.",
                       "- Содержание сохранено на дату исследования; публикация не обновляет факты и не делает новых запросов.",
                       "- Точная привязка фактов к источникам, исходный отчёт и даты сохранены в архиве исследования."])
        lines.extend("- " + warning for warning in warnings)
        lines.extend(["", "## Источники", "",
                      "Источники перечислены по порядку использования, без числовых ссылок в тексте. Точная привязка утверждений и расчётов сохранена в архиве исследования.", "",
                      "Время получения не равно дате наблюдения или действия правил. Недатированные документы не считаются обновлёнными в момент загрузки. Пропуски и ошибки не означают нулевую активность.", ""])
        lines.extend(self.bibliography())
        if source_notes:
            lines.extend(["### Примечания к источникам", "", source_notes, ""])
        return "\n".join(lines)


def cleanup_legacy_exports(output, stem):
    """Remove only unmodified generated reports from the old hash naming scheme."""
    removed, warnings = [], []
    for path in sorted(output.glob(stem + "--*.md")):
        match = re.fullmatch(re.escape(stem) + r"--([a-f0-9]{12})\.md", path.name)
        if not match or path.is_symlink():
            continue
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as file:
                before = os.fstat(file.fileno())
                if not stat.S_ISREG(before.st_mode):
                    continue
                data = file.read(core.MAX_BODY + 1)
            if len(data) > core.MAX_BODY:
                warnings.append("Слишком большой прежний экспорт сохранён: " + path.name)
                continue
            generated = (data.startswith(b"# ") and "**Исследование · ".encode() in data
                         and "### Проверка публикации".encode() in data)
            if not generated or not hashlib.sha256(data).hexdigest().startswith(match[1]):
                warnings.append("Сохранён изменённый или посторонний файл: " + path.name)
                continue
            after = path.stat(follow_symlinks=False)
            unchanged = all(getattr(before, field) == getattr(after, field)
                            for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns"))
            if not unchanged:
                warnings.append("Файл изменился во время проверки и сохранён: " + path.name)
                continue
            path.unlink()
            removed.append(path.name)
        except FileNotFoundError:
            continue
        except OSError:
            warnings.append("Не удалось очистить прежний экспорт: " + path.name)
    return removed, warnings


def publish_report(run, *, project_root=None):
    root = Path.cwd() if project_root is None else Path(project_root)
    core.no_symlinks(root)
    root = root.resolve()
    directory = core.run_directory(run, root)
    result = core.check(directory, final=True, _snapshot=True)
    snapshot = result.pop("_snapshot", None)
    if not result["ok"]:
        return {"ok": False, "final_check": result, "error": "Final check failed; report was not published"}
    content = Renderer(snapshot).render(result).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    date = datetime.fromisoformat(snapshot["manifest"]["created_at"]).date().isoformat()
    stem = project_slug(snapshot["manifest"]["identity"]["name"]) + "-" + date
    output = root / "reports"
    core.no_symlinks(output)
    output.mkdir(exist_ok=True)
    destination = output / (stem + ".md")
    core.no_symlinks(destination)
    if destination.exists() and not destination.is_file():
        raise core.ResearchError("Report destination must be a regular file")
    reused = destination.exists() and destination.read_bytes() == content
    if not reused:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=output, prefix=".publish-", delete=False) as file:
                temporary = Path(file.name)
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            core.no_symlinks(destination)
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    removed, warnings = cleanup_legacy_exports(output, stem)
    return {"ok": True, "report_path": str(destination), "run_dir": str(directory),
            "sha256": digest, "source_report_sha256": snapshot["report_sha256"],
            "already_published": reused, "removed_legacy_reports": removed,
            "cleanup_warnings": warnings, "final_check": result}
