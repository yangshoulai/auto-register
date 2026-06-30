from pathlib import Path
import sysconfig


# 当前项目的业务包名为 email。为了降低和 Python 标准库 email 包同名的影响，
# 这里把标准库 email 目录也加入搜索路径，保证 email.message 等子模块仍可导入。
_stdlib_email_path = Path(sysconfig.get_path("stdlib")) / "email"
if _stdlib_email_path.exists():
    __path__.append(str(_stdlib_email_path))


def message_from_string(s, *args, **kwargs):
    """
    兼容标准库 email.message_from_string。
    """
    from email.parser import Parser

    return Parser(*args, **kwargs).parsestr(s)


def message_from_bytes(s, *args, **kwargs):
    """
    兼容标准库 email.message_from_bytes。
    """
    from email.parser import BytesParser

    return BytesParser(*args, **kwargs).parsebytes(s)


def message_from_file(fp, *args, **kwargs):
    """
    兼容标准库 email.message_from_file。
    """
    from email.parser import Parser

    return Parser(*args, **kwargs).parse(fp)


def message_from_binary_file(fp, *args, **kwargs):
    """
    兼容标准库 email.message_from_binary_file。
    """
    from email.parser import BytesParser

    return BytesParser(*args, **kwargs).parse(fp)
