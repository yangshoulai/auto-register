import logging
import random
import string
from dataclasses import dataclass

from core.config import AccountServiceConfig, settings

logger = logging.getLogger(__name__)


@dataclass
class Account:
    first_name: str
    last_name: str
    age: int
    password: str
    email_address: str | None = None
    mobile: str | None = None


AccountCreationConfig = AccountServiceConfig

FIRST_NAMES = (
    "James",
    "Robert",
    "John",
    "Michael",
    "David",
    "William",
    "Richard",
    "Joseph",
    "Thomas",
    "Christopher",
    "Mary",
    "Patricia",
    "Jennifer",
    "Linda",
    "Elizabeth",
    "Barbara",
    "Susan",
    "Jessica",
    "Sarah",
    "Karen",
)

LAST_NAMES = (
    "Smith",
    "Johnson",
    "Williams",
    "Brown",
    "Jones",
    "Garcia",
    "Miller",
    "Davis",
    "Rodriguez",
    "Martinez",
    "Hernandez",
    "Lopez",
    "Gonzalez",
    "Wilson",
    "Anderson",
    "Thomas",
    "Taylor",
    "Moore",
    "Jackson",
    "Martin",
)

PASSWORD_SPECIAL_CHARS = "!@#$%^&*()-_=+[]{}:,.?"
PASSWORD_LENGTH = 12
MIN_AGE = 20
MAX_AGE = 45


class AccountService:
    def __init__(self, config: AccountCreationConfig | None = None) -> None:
        self._config = config or settings.account_service
        self._random = random.SystemRandom()

    def create_account(self, config: AccountCreationConfig | None = None) -> Account:
        """
        创建账号初始数据。
        """
        account_config = config or self._config
        account = Account(
            first_name=self._random.choice(FIRST_NAMES),
            last_name=self._random.choice(LAST_NAMES),
            age=self._random.randint(MIN_AGE, MAX_AGE),
            password=account_config.specified_password or self._create_password(),
        )
        logger.info(
            "账号基础资料已创建: name=%s %s, age=%s, password_source=%s",
            account.first_name,
            account.last_name,
            account.age,
            "config" if account_config.specified_password else "generated",
        )
        return account

    def _create_password(self) -> str:
        character_groups = (
            string.ascii_lowercase,
            string.ascii_uppercase,
            string.digits,
            PASSWORD_SPECIAL_CHARS,
        )
        password_chars = [
            self._random.choice(character_group)
            for character_group in character_groups
        ]
        all_characters = "".join(character_groups)

        password_chars.extend(
            self._random.choice(all_characters)
            for _ in range(PASSWORD_LENGTH - len(password_chars))
        )
        self._random.shuffle(password_chars)

        return "".join(password_chars)
