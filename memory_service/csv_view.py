"""Deterministic, bounded CSV view; source bytes and original metadata never change."""
import os
import re

from .safe_fs import Fault
from .store import MAX_BYTES, digest, label

PARSER_VERSION = 'csv-utf8-v1'
LIMITS = dict(sourceBytes=262144, records=1001, columns=64, cellBytes=32768)


class ParseFailure(Exception):
    def __init__(self, code, message, **details):
        self.error = dict(code=code, message=message, **details)


def parse_csv(data, filename, version):
    result = dict(parserVersion=PARSER_VERSION, sourceSHA=digest(data), sourceVersion=version,
                  parseStatus='failed', limits=dict(LIMITS))
    try:
        if os.path.splitext(filename)[1].lower() != '.csv':
            raise ParseFailure('not-csv', '只支持明确的 .csv 文件。')
        if len(data) > LIMITS['sourceBytes']:
            raise ParseFailure('csv-limit-exceeded', '原文已保存，超过表格解析字节上限。', limit='sourceBytes')
        try:
            raw = data.decode('utf-8')
        except UnicodeDecodeError:
            raise ParseFailure('invalid-utf8', '内容不是有效 UTF-8；不猜测编码。')
        text = raw[1:] if raw.startswith('\ufeff') else raw
        if not text:
            raise ParseFailure('empty-csv', 'CSV 没有表头记录。')
        records, cells, field = [], [], []
        cell_bytes, line, start, i = 0, 1, 1, 0
        state = 'start'  # start / unquoted / quoted / closed
        active = False

        def fail(code, message, **details):
            raise ParseFailure(code, message, recordNumber=len(records) + 1,
                               lineStart=start, lineEnd=line, **details)

        def append(value):
            nonlocal cell_bytes
            cell_bytes += len(value.encode('utf-8'))
            if cell_bytes > LIMITS['cellBytes']:
                fail('csv-limit-exceeded', '单元格超过 UTF-8 字节上限。', limit='cellBytes')
            field.append(value)

        def end_cell():
            nonlocal field, cell_bytes, state
            if len(cells) >= LIMITS['columns']:
                fail('csv-limit-exceeded', '记录超过列数上限。', limit='columns')
            cells.append(''.join(field))
            field, cell_bytes, state = [], 0, 'start'

        def end_record():
            nonlocal cells, active
            if not active:
                fail('invalid-csv', '不接受引号外的空记录。')
            end_cell()
            if len(records) >= LIMITS['records']:
                fail('csv-limit-exceeded', '超过逻辑记录数上限（含表头）。', limit='records')
            if records and len(cells) != len(records[0]['cells']):
                fail('column-count-mismatch', '数据记录列数与表头不同；未补齐或丢弃字段。')
            records.append(dict(recordNumber=len(records) + 1, lineStart=start, lineEnd=line, cells=cells))
            cells, active = [], False

        while i < len(text):
            char = text[i]
            if char == '\x00':
                fail('invalid-csv', 'CSV 不接受 NUL 字符。')
            if char == '\r':
                if i + 1 >= len(text) or text[i + 1] != '\n':
                    fail('invalid-csv', '只接受 LF 或 CRLF 换行，不接受裸 CR。')
                newline, step = '\r\n', 2
            elif char == '\n':
                newline, step = '\n', 1
            else:
                newline, step = None, 1
            if newline is not None:
                if state == 'quoted':
                    append(newline)
                else:
                    end_record()
                line += 1
                if state != 'quoted': start = line
                i += step
                continue
            active = True
            if state == 'quoted':
                if char == '"': state = 'closed'
                else: append(char)
            elif state == 'closed':
                if char == '"':
                    append('"'); state = 'quoted'
                elif char == ',': end_cell()
                else: fail('invalid-csv', '关闭引号后只能接分隔符、换行或文件结束。')
            elif char == ',':
                end_cell()
            elif char == '"':
                if state != 'start': fail('invalid-csv', '双引号只能位于字段开头。')
                state = 'quoted'
            else:
                append(char); state = 'unquoted'
            i += 1
        if state == 'quoted': fail('invalid-csv', '引号字段没有闭合。')
        if active: end_record()
        result.update(parseStatus='parsed', rawText=raw, header=records[0], rows=records[1:],
                      columnCount=len(records[0]['cells']), dataRecordCount=len(records) - 1)
    except ParseFailure as exc:
        result['error'] = exc.error
    return result


def table_view(vault, rid, version, project):
    label(project, 'project')
    if not re.fullmatch(r'[1-9][0-9]{0,6}', version):
        raise Fault('INVALID_VERSION', '表格版本必须是明确的正整数。')
    with vault.lock:
        record = vault.get(rid, version)['record']
        if record['project'] != project:
            raise Fault('NOT_FOUND', '没有所请求的来源记录或版本', 404)
        data = vault.fs.read(record['path'], MAX_BYTES)
        if digest(data) != record['sha256']:
            raise Fault('SOURCE_INTEGRITY', '来源校验失败，未提供表格。', 409)
        return dict(schema=1, record=record, table=parse_csv(data, record['filename'], record['version']))
