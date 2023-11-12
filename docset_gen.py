#!/usr/bin/env python3
"""Pebble SDK Docset generator for DASH.

Usage:
  docset_gen.py (--aplite | --basalt) INPUT_PATH [-o DOCSET_PATH]
  docset_gen.py (-h | --help)

Arguments:
  INPUT_PATH                   Input docset path.

Options:
  -h --help                    Show this screen.
  -o DOCSET_PATH, --output-path DOCSET_PATH  Output docset path.
"""
from __future__ import print_function

import os
import io
import re
import glob
import sqlite3
import shutil

from itertools import groupby
from collections import namedtuple
from operator import itemgetter

from typing import Iterable, Tuple
import bs4
from docopt import docopt

DOC_PATH = 'pebble-sdk.docset/Contents/Resources/Documents'

INSERT_QUERY_TEMPLATE = """
    INSERT OR IGNORE INTO searchIndex(name, type, path)
    VALUES (?,?,?)
"""

INFO_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>pebble-sdk</string>
    <key>CFBundleName</key>
    <string>Pebble SDK ({arch})</string>
    <key>DocSetPlatformFamily</key>
    <string>pebble</string>
    <key>isDashDocset</key>
    <true/>
    <key>isJavaScriptEnabled</key>
    <true/>
    <key>dashIndexFilePath</key>
    <string>{arch}/modules.html</string>
</dict>
</plist>
"""

join = os.path.join

SearchIndexData = namedtuple('SearchIndexData', ['name', 'type', 'path'])

class HTMLParser:
    """ Doxygen HTML file parser
    """
    Result = namedtuple('Result', ['name', 'type', 'path'])

    section_type_map = {
        'Function Documentation': 'Function',
        'Data Structure Documentation': 'Struct',
        'Typedef Documentation': 'Type',
        'Enumeration Type Documentation': 'Enum',
        'Macro Definition Documentation': 'Macro',
    }

    sections_to_skip = [
        'Detailed Description',
        None,
    ]

    def __init__(self, content, path):
        self.content = content
        self.path = path
        self.soup = bs4.BeautifulSoup(content, 'html.parser')

    def sections_elements_iterator(self) -> Iterable[Tuple[str, bs4.PageElement]]:
        contents_el = self.soup.find('div', attrs={'class': 'contents'})

        group_name = None
        for el in contents_el:
            if is_tag(el) and is_group_header(el):
                group_name = el.string
            yield (group_name, el)

    def parse_section(self, name: str, elements: Iterable[bs4.PageElement]) -> Iterable[SearchIndexData]:
        if name in self.sections_to_skip:
            return

        type_ = self.section_type_map[name]

        anchor_id = None
        for element in elements:
            if is_anchor(element):
                anchor_id = element.get('id')
                continue
            if not is_declaration(element):
                continue

            table_el = (element
                .find('div', attrs={'class': 'memproto'})
                .find('table', attrs={'class': 'memname'})
            )

            declaration = table_el.text

            obj_name = parse_declaration(type_=type_, text=declaration)

            path = self.path
            if anchor_id:
                path += f"#{str(anchor_id)}"

            yield SearchIndexData(obj_name, type_, path)

    def parse(self) -> Iterable[Result]:
        for section_name, group in groupby(
                self.sections_elements_iterator(),
                key=itemgetter(0),
                ):
            elements = [t[1] for t in group]
            yield from self.parse_section(section_name, elements)


regexs = {
    'last_word': re.compile(r'.*?(?P<name>\w+)\s*$', re.UNICODE | re.DOTALL),
    'macro': re.compile(r'''
            ^\s*
            \#define\s+
            (?P<name>\w+)\s+
            .*
            $
        ''', re.VERBOSE | re.UNICODE | re.DOTALL),
    'function': re.compile(r'''
            ^
            \s*
            (?:(?P<fn_modifier>\w+)\s+)?
            (?P<fn_type>\w+(?:\s*\*)?)\s+
            (?P<fn_name>\w+)\s*
            \(
            .*
            $
        ''', re.VERBOSE | re.UNICODE | re.DOTALL),
    'typedef_function': re.compile(r'''
            ^\s*
            typedef\s+
            (?P<fn_type>\w+)\s*
            \(
                \*\s+
                (?P<fn_name>\w+)\s*
            \)
            \(
            .*
            $
        ''', re.VERBOSE | re.UNICODE | re.DOTALL),
    'typedef': re.compile(r'''
            ^\s*
            typedef\s+
            .*?
            (?P<name>\w+)\s*
            $
        ''', re.VERBOSE | re.UNICODE | re.DOTALL),
}


def parse_declaration(type_: str, text: str) -> str:
    if type_ == 'Function':
        match = regexs['function'].match(text)
        result = match.group('fn_name')
    elif type_ == 'Macro':
        match = regexs['macro'].match(text)
        result = match.group('name')
    elif type_ in {'Struct', 'Enum'}:
        match = regexs['last_word'].match(text)
        result = match.group('name')
    elif type_ == 'Type':
        match_fn = regexs['typedef_function'].match(text)
        match = regexs['typedef'].match(text)
        match_last_word = regexs['last_word'].match(text)
        if match_fn:
            result = match_fn.group('fn_name')
        elif match:
            result = match.group('name')
        else:
            result = match_last_word.group('name')

    return result


def is_tag(element: bs4.PageElement) -> bool:
    return element.name is not None


def is_group_header(element: bs4.PageElement) -> bool:
    return is_tag(element) and 'groupheader' in element.get('class', ())


def is_anchor(element: bs4.PageElement) -> bool:
    return (is_tag(element)
        and element.name == 'a'
        # and 'anchor' in element.get('class', ())
    )


def is_declaration(element: bs4.PageElement) -> bool:
    return (is_tag(element)
        and element.name == 'div' and 'memitem' in element.get('class', ())
    )


def take_db(path: str) -> sqlite3.Connection:
    """ Drop and create database at the given path
    :param path: database file path
    :return: the database
    """
    db = sqlite3.connect(path)
    with db:
        try:
            db.execute('DROP TABLE searchIndex;')
        except:
            print("`searchIndex` table did not exist.")

        db.execute('''
            CREATE TABLE searchIndex(
                id INTEGER PRIMARY KEY,
                name TEXT,
                type TEXT,
                path TEXT
            );
            '''
        )
        db.execute('''
            CREATE UNIQUE INDEX anchor ON searchIndex (name, type, path);
            '''
        )

    return db


def db_path(start: str) -> str:
    return join(start, 'Contents', 'Resources', 'docSet.dsidx')


def setup_tree(input_path:str, output_path: str) -> None:
    """ Setup the output DASH directory
    :param input_path: Pebble SDK Documentation path
    :param output_path: DASH docset path
    """
    shutil.rmtree(output_path, ignore_errors=True)
    os.makedirs(join(output_path, 'Contents', 'Resources'))

    shutil.copytree(input_path,
                    join(output_path, 'Contents', 'Resources', 'Documents'))
    shutil.copy('icon.png', output_path)
    shutil.copy('icon@2x.png', output_path)

def init_plist(docset_path: str, arch: str):
    with io.open(join(docset_path, 'Contents', 'Info.plist'),
                 'wt', encoding='utf-8') as output_f:
        output_f.write(INFO_PLIST_TEMPLATE.format(arch=arch))


def main() -> None:
    arguments = docopt(__doc__)

    arch = 'basalt' if arguments['--basalt'] else 'aplite'
    input_path = arguments['INPUT_PATH']
    docset_path = arguments['--output-path'] or f'pebble-sdk-{arch}.docset'

    documents_path = join(docset_path, 'Contents', 'Resources', 'Documents')

    setup_tree(input_path, docset_path)
    init_plist(docset_path, arch)

    db = take_db(db_path(start=docset_path))

    file_path_list = glob.glob(join(documents_path, arch.capitalize(), 'group__*.html'))

    insert_data_list = []
    for file_path in file_path_list:
        relpath = os.path.relpath(file_path, start=documents_path)
        with io.open(file_path, 'rt', encoding='utf-8') as input_f:
            p = (HTMLParser(input_f, relpath)
                .parse()
            )
            insert_data_list.extend(iter(p))
    with db:
        for t in insert_data_list:
            print("Inserting ", t)
            db.execute(INSERT_QUERY_TEMPLATE, (t.name, t.type, t.path))

if __name__ == '__main__':
    main()
