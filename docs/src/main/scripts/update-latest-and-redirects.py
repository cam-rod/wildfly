# Copyright 2023 Red Hat, Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import re
import os
import shutil
import sys
from argparse import ArgumentParser, Namespace
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Tuple, Optional
from pathlib import Path, PurePath
from xml.etree import ElementTree
from xml.etree.ElementTree import Element

from bs4 import BeautifulSoup

REMOVED_REDIRECT_TEMPLATE: Tuple[str, str, str, str, str, str] = (
    """<!DOCTYPE html>
<html lang=\"en\">
  <head>
    <meta charset=\"UTF-8\">
    <meta http-equiv=\"refresh\" content=\"3; url=""",
    """\">
    <link rel=\"canonical\" href=\"""",
    """\" />
  </head>
  <body>
    <p>The page does not exist in this version (""",
    """). You will be redirected to the last available version in 3 seconds.</p>
    <p>If the page doesn't open, click the following link: <a href=\"""",
    """\">""",
    """</a></p>
  </body>
</html>""",
)

REL_PATH_PATTERN = re.compile(r"(/)?([^/]+)")

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


@dataclass
class HTMLTagMatch:
    """Stores the location of a single-line tag (and regex match, if multiple tags are present on one line)"""

    line: Optional[int]
    col: Optional[int]

    def __init__(self) -> None:
        self.line = None
        self.col = None

    def set_match(self, line: int, col: int) -> None:
        """Set the line and match, if not previously set."""
        if self.line is None:
            self.line = line
            self.col = col


def move_directories(
    base_docs_path: Path,
    latest_docs_path: PurePath,
    args: Namespace,
) -> None:
    """Adjust names and location of the directories.

    :param base_docs_path: base directory for documentation
    :param latest_docs_path: directory for `latest` docs. If the directory already exists, it is deleted, and the source
    directory takes its place
    :param args: paths and versions for current and previous documentation
    """
    if Path(latest_docs_path).exists():
        shutil.rmtree(latest_docs_path)

    os.rename(args.source, latest_docs_path)
    if (versioned_path := base_docs_path.joinpath(args.source_version)).exists():
        shutil.rmtree(versioned_path)


def rel_path_replace(matched: re.Match) -> str:
    """Replaces path elements with ".." for relative URLs

    :param matched: the match element
    :return: a replacement for ascending levels in a relative path
    """
    replacement: str = ""
    for m in matched.groups():
        if m is not None:
            replacement += ".." if os.sep not in m else os.sep

    return replacement


def generate_canonical_url(
    base_docs_path: Path,
    canon_file: PurePath,
    canon_dir_path: PurePath,
    current_dirname: str,
) -> Tuple[PurePath, str]:
    """Returns relative URLs for two files with identical paths, except for one level

    :param base_docs_path: base directory for documentation. Both files have an identical path to this point
    :param canon_file: full path of the canonical file
    :param canon_dir_path: full path of the directory immediately below :py:data:`base_docs_path`
        containing :py:data:`canon_file`
    :param current_dirname: name of the directory immediately below :py:data:`base_docs_path`
        containing the current file
    """
    redirect_file = base_docs_path.joinpath(current_dirname).joinpath(
        canon_file.relative_to(canon_dir_path)
    )

    # Generated a relative path via the base directory (ex. "../../..")
    redirect_url = PurePath(
        re.sub(
            REL_PATH_PATTERN,
            rel_path_replace,
            str(redirect_file.relative_to(base_docs_path)),
        )
    )
    redirect_url = redirect_url.joinpath(canon_file.relative_to(base_docs_path))

    return redirect_file, str(redirect_url).replace(os.sep, "/")


def create_canonical_tag(
    base_docs_path: Path,
    current_docs_path: PurePath,
    current_file: PurePath,
    version: str,
    use_abs_path: bool,
) -> None:
    """Create a canonical link tag for a new documentation page.

    :param base_docs_path: base directory for documentation
    :param current_docs_path: directory with documentation being checked
    :param current_file: the file a redirect is being created for
    :param version: name of the version to use in the canonical link
    :param use_abs_path: sets redirects to use relative or absolute paths
    """
    versioned_file, canonical_url = generate_canonical_url(
        base_docs_path, current_file, current_docs_path, version
    )
    if use_abs_path:
        canonical_url = "/" + str(current_file.relative_to(base_docs_path)).replace(
            os.sep, "/"
        )
    canonical_tag = f'<link rel="canonical" href="{canonical_url}">'

    with open(current_file, "r") as c_file_io:
        c_file = c_file_io.readlines()

    # Also record/delete the Match, if HTML elements are collapsed onto a single line
    existing_canon_tags: list[int] = []

    head_closing_tag = HTMLTagMatch()

    for idx, line in enumerate(c_file):
        char_offset = 0  # Count chars removed for same line canon links
        # Don't edit redirect files
        if re.search("meta.*http-equiv=[\"']refresh[\"']", line):
            return

        if re.match("^\\s*<link rel=[\"']canonical[\"'].*?>\n$", line):
            existing_canon_tags.append(idx)
        elif line_tags := re.findall("<link rel=[\"']canonical[\"'].*?>\\s*", line):
            char_offset = sum([len(ch) for ch in line_tags])
            c_file[idx] = re.sub("<link rel=[\"']canonical[\"'].*?>\\s*", "", line)

        if re.match("^\\s*</head>\n$", line) and head_closing_tag.line is None:
            head_closing_tag.line = idx
        elif head_close_match := re.search("</head>\\s*", line):
            # HTML elements are collapsed onto a single line
            head_closing_tag.set_match(idx, head_close_match.start(0) - char_offset)

    if head_closing_tag.line is None:
        print(
            f"Unable to update canonical link for {current_file} due to missing </head> tag"
        )
        return

    for idx in sorted(existing_canon_tags, reverse=True):
        del c_file[idx]
        if idx < head_closing_tag.line:
            head_closing_tag.line -= 1

    if head_closing_tag.col is not None:
        c_file[head_closing_tag.line] = (
            c_file[head_closing_tag.line][: head_closing_tag.col]
            + canonical_tag
            + " "
            + c_file[head_closing_tag.line][head_closing_tag.col :]
        )
    else:
        c_file.insert(head_closing_tag.line, canonical_tag + "\n")

    with open(current_file, "w") as c_file_io:
        c_file_io.writelines(c_file)


def redirect_removed_file(
    base_docs_path: Path,
    previous_docs_file: PurePath,
    current_dirname: str,
    previous_dirname: str,
    current_version: str,
    use_abs_path: bool,
    product_name: str | None,
) -> None:
    """Create a redirect for a page found in the previous version, but not the current.

    This method also propagates static redirects found in the previous version.

    :param base_docs_path: base directory for documentation
    :param previous_docs_file: path of a file present in the previous version
    :param current_dirname: name of the directory with the current version (usually `latest` at this stage)
    :param previous_dirname: name of the directory with the previous version
    :param current_version: name of the current version
    :param use_abs_path: sets redirects to use relative or absolute paths
    :param product_name: name of the product, if provided
    """
    redirect_file, redirect_url = generate_canonical_url(
        base_docs_path,
        previous_docs_file,
        base_docs_path.joinpath(previous_dirname),
        current_dirname,
    )
    if use_abs_path:
        redirect_url = "/" + str(
            previous_docs_file.relative_to(base_docs_path)
        ).replace(os.sep, "/")

    if not Path(redirect_file).exists():
        redirect_file_contents: str

        # Extract existing static redirect in the "canonical" page, and use as redirect instead
        with open(previous_docs_file, "r") as prev_file_io:
            prev_file = BeautifulSoup(prev_file_io.read(), "html.parser")
            if static_redirect := prev_file.find(
                "meta", attrs={"http-equiv": "refresh"}
            ):
                canon_link = prev_file.find("link", rel="canonical")

                if canon_link and canon_link["href"] in static_redirect["content"]:
                    redirect_url = canon_link["href"]

        if product_name:
            redirect_file_contents = (
                redirect_url.join(REMOVED_REDIRECT_TEMPLATE[0:3])
                + f"{product_name} {current_version}"
                + redirect_url.join(REMOVED_REDIRECT_TEMPLATE[3:6])
            )
        else:
            redirect_file_contents = (
                redirect_url.join(REMOVED_REDIRECT_TEMPLATE[0:3])
                + str(current_version)
                + redirect_url.join(REMOVED_REDIRECT_TEMPLATE[3:6])
            )

        os.makedirs(redirect_file.parent, exist_ok=True)
        with open(redirect_file, "w") as red_file_io:
            red_file_io.write(redirect_file_contents)


def canonical_tag_walker(
    base_docs_path: Path, current_docs_path: PurePath, version: str, use_abs_path: bool
) -> None:
    """Walk the files contained within the "latest/" directory and create canonical link tags.

    Links are only created for HTML pages, since they do not work with images, stylesheets, etc.

    :param base_docs_path: base directory for documentation
    :param current_docs_path: directory with documentation being checked
    :param version: name of the version to use in the canonical link
    :param use_abs_path: sets redirects to use relative or absolute paths
    """

    for parent, dirs, files in os.walk(current_docs_path):
        for f in files:
            if f.endswith(".html"):
                create_canonical_tag(
                    base_docs_path,
                    current_docs_path,
                    PurePath(parent, f),
                    version,
                    use_abs_path,
                )


def redirect_removed_walker(
    base_docs_path: Path,
    current_dirname: str,
    previous_dirname: str,
    current_version: str,
    use_abs_path: bool,
    product_name: str | None,
) -> None:
    """Create redirects from the previous version of the documentation, if pages have been removed.

    :param base_docs_path: base directory for documentation
    :param current_dirname: name of the directory with the current version (usually `latest` at this stage)
    :param previous_dirname: name of the directory with the previous version
    :param current_version: name of the current version
    :param use_abs_path: sets redirects to use relative or absolute paths
    :param product_name: name of the product, if provided
    """
    for parent, dirs, files in os.walk(base_docs_path.joinpath(previous_dirname)):
        for f in files:
            if f.endswith("html"):
                redirect_removed_file(
                    base_docs_path,
                    PurePath(parent, f),
                    current_dirname,
                    previous_dirname,
                    current_version,
                    use_abs_path,
                    product_name,
                )


def update_sitemap(base_docs_path: Path) -> None:
    """Update a file sitemap.xml, located in the same folder as :py:data:`base_docs_path`.

    Sets `lastmod` on all URLs to today's date.

    :param base_docs_path: base directory, containing sitemap.xml
    """
    ElementTree.register_namespace("", SITEMAP_NS)
    revdate = str(datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    sitemap_path = base_docs_path.joinpath("sitemap.xml")
    tree = ElementTree.parse(sitemap_path)

    for link in tree.getroot().findall(f"{{{SITEMAP_NS}}}url"):
        for lastmod in link.findall(f"{{{SITEMAP_NS}}}lastmod"):
            link.remove(lastmod)

        lastmod = Element(f"{{{SITEMAP_NS}}}lastmod")
        lastmod.text = revdate
        link.insert(1, lastmod)

    ElementTree.indent(tree)
    tree.write(
        sitemap_path,
        xml_declaration=True,
        encoding="utf-8",
    )

    with open(sitemap_path, "r") as sitemap:
        decl = sitemap.readline()
        remainder = sitemap.readlines()

    decl = decl.replace("'", '"')
    decl_end = decl.rfind("?")
    decl = decl[:decl_end] + ' standalone="yes" ' + decl[decl_end:]
    remainder.insert(0, decl)

    with open(sitemap_path, "w") as sitemap:
        sitemap.writelines(remainder)


def setup_argparse() -> ArgumentParser:
    """Loads args into the argument parser.

    :return: The argument parser
    """
    parser = ArgumentParser(
        prog="update-latest-and-redirects",
        description="Use `latest` as a canonical path for current documentation. (ex. wildfly.org/latest/)."
        + " All paths are relative to the parent directory of -s, --source.",
    )
    parser.add_argument(
        "-s",
        "--source",
        metavar="directory",
        help="Required. Directory containing the latest documentation. This directory will be renamed to `latest`. "
        + "Any existing directory named `latest` will be removed (see -p, --previous).",
        type=lambda p: Path(p).resolve(),
        required=True,
    )
    parser.add_argument(
        "-sv",
        "--source-version",
        metavar="version",
        help="Required. Version of the latest documentation (ex. 28). To be used for versioned documentation. "
        + " Any existing directory with this name will be deleted.",
        required=True,
    )
    parser.add_argument(
        "-pv",
        "--previous-version",
        metavar="version",
        help="Versioned directory of the previous documentation (ex. 27). To be used for redirecting to pages not"
        + "present in the latest version.",
    )
    parser.add_argument(
        "--no-absolute-path",
        help="Do not treat the parent directory of -s, --source as a top level path element when generating redirects. "
        + "Ex. a source directory at latest-new/ will create a redirect of the form `../latest/index.html` "
        + "instead of `/latest/index.html`. This may break some redirects.",
        dest="absolute_path",
        action="store_false",
    )
    parser.add_argument(
        "--product",
        metavar="name",
        help="Name of the product (ex. WildFly), used on redirect pages.",
    )
    parser.add_argument(
        "--update-sitemap",
        help="Update a file sitemap.xml, located in the same folder as -s, --source.  Sets `lastmod` on all URLs "
        + "to today's date.",
        action="store_true",
    )

    return parser


def main():
    args = setup_argparse().parse_args()

    if not args.source.exists():
        print(f"{args.source} is not a valid directory", file=sys.stderr)
        sys.exit(1)

    base_docs_path = Path(args.source.parent)
    latest_docs_path = base_docs_path.joinpath("latest")

    move_directories(base_docs_path, latest_docs_path, args)
    canonical_tag_walker(base_docs_path, latest_docs_path, "latest", args.absolute_path)

    if args.previous_version:
        previous_docs_path = base_docs_path.joinpath(args.previous_version)
        canonical_tag_walker(
            base_docs_path,
            previous_docs_path,
            args.previous_version,
            args.absolute_path,
        )
        redirect_removed_walker(
            base_docs_path,
            "latest",
            args.previous_version,
            args.source_version,
            args.absolute_path,
            args.product,
        )

    # Create versioned directory with the same canonical links
    shutil.copytree(latest_docs_path, base_docs_path.joinpath(args.source_version))

    if args.update_sitemap:
        update_sitemap(base_docs_path)


main()
