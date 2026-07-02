from __future__ import annotations

import unittest

from main import build_register_flow
from register.nodes import (
    AddPhoneNumberNode,
    FillAboutYouNode,
    FillEmailAndSubmitNode,
    OpenChatGptTabNode,
    SelectCodexAccountNode,
    SubmitCodexConsentNode,
    WaitEmailVerificationCodeNode,
    WaitSmsVerificationCodeNode,
)
from register.register_flow import NodeResult, RegisterContext


class MainTest(unittest.TestCase):
    def test_build_register_flow_adds_register_nodes(self) -> None:
        flow = build_register_flow()

        self.assertEqual(flow.start_node, OpenChatGptTabNode.DEFAULT_NAME)
        self.assertIn(OpenChatGptTabNode.DEFAULT_NAME, flow.nodes)
        self.assertIn(FillEmailAndSubmitNode.DEFAULT_NAME, flow.nodes)
        self.assertIn(WaitEmailVerificationCodeNode.DEFAULT_NAME, flow.nodes)
        self.assertIn(FillAboutYouNode.DEFAULT_NAME, flow.nodes)
        self.assertIn(SelectCodexAccountNode.DEFAULT_NAME, flow.nodes)
        self.assertIn(AddPhoneNumberNode.DEFAULT_NAME, flow.nodes)
        self.assertIn(WaitSmsVerificationCodeNode.DEFAULT_NAME, flow.nodes)
        self.assertIn(SubmitCodexConsentNode.DEFAULT_NAME, flow.nodes)
        self.assertIsInstance(
            flow.nodes[OpenChatGptTabNode.DEFAULT_NAME],
            OpenChatGptTabNode,
        )
        self.assertIsInstance(
            flow.nodes[FillEmailAndSubmitNode.DEFAULT_NAME],
            FillEmailAndSubmitNode,
        )
        self.assertIsInstance(
            flow.nodes[WaitEmailVerificationCodeNode.DEFAULT_NAME],
            WaitEmailVerificationCodeNode,
        )
        self.assertIsInstance(
            flow.nodes[FillAboutYouNode.DEFAULT_NAME],
            FillAboutYouNode,
        )
        self.assertIsInstance(
            flow.nodes[SelectCodexAccountNode.DEFAULT_NAME],
            SelectCodexAccountNode,
        )
        self.assertIsInstance(
            flow.nodes[AddPhoneNumberNode.DEFAULT_NAME],
            AddPhoneNumberNode,
        )
        self.assertIsInstance(
            flow.nodes[WaitSmsVerificationCodeNode.DEFAULT_NAME],
            WaitSmsVerificationCodeNode,
        )
        self.assertIsInstance(
            flow.nodes[SubmitCodexConsentNode.DEFAULT_NAME],
            SubmitCodexConsentNode,
        )

    def test_build_register_flow_connects_register_nodes(self) -> None:
        flow = build_register_flow()
        ctx = RegisterContext()

        self.assertEqual(
            flow.find_next_node(
                OpenChatGptTabNode.DEFAULT_NAME,
                result=NodeResult.ok(status=OpenChatGptTabNode.SUCCESS_STATUS),
                ctx=ctx,
            ),
            FillEmailAndSubmitNode.DEFAULT_NAME,
        )
        self.assertEqual(
            flow.find_next_node(
                FillEmailAndSubmitNode.DEFAULT_NAME,
                result=NodeResult.ok(status=FillEmailAndSubmitNode.SUCCESS_STATUS),
                ctx=ctx,
            ),
            WaitEmailVerificationCodeNode.DEFAULT_NAME,
        )
        self.assertEqual(
            flow.find_next_node(
                FillEmailAndSubmitNode.DEFAULT_NAME,
                result=NodeResult.ok(
                    status=FillEmailAndSubmitNode.SMS_VERIFICATION_READY_STATUS
                ),
                ctx=ctx,
            ),
            WaitSmsVerificationCodeNode.DEFAULT_NAME,
        )
        self.assertEqual(
            flow.find_next_node(
                WaitEmailVerificationCodeNode.DEFAULT_NAME,
                result=NodeResult.ok(
                    status=WaitEmailVerificationCodeNode.RETRY_CURRENT_NODE_STATUS
                ),
                ctx=ctx,
            ),
            WaitEmailVerificationCodeNode.DEFAULT_NAME,
        )
        self.assertEqual(
            flow.find_next_node(
                WaitEmailVerificationCodeNode.DEFAULT_NAME,
                result=NodeResult.ok(status=WaitEmailVerificationCodeNode.SUCCESS_STATUS),
                ctx=ctx,
            ),
            FillAboutYouNode.DEFAULT_NAME,
        )
        self.assertEqual(
            flow.find_next_node(
                WaitEmailVerificationCodeNode.DEFAULT_NAME,
                result=NodeResult.ok(
                    status=WaitEmailVerificationCodeNode.CHATGPT_READY_STATUS
                ),
                ctx=ctx,
            ),
            SelectCodexAccountNode.DEFAULT_NAME,
        )
        self.assertEqual(
            flow.find_next_node(
                WaitEmailVerificationCodeNode.DEFAULT_NAME,
                result=NodeResult.ok(
                    status=WaitEmailVerificationCodeNode.CODEX_NEEDS_PHONE_STATUS
                ),
                ctx=ctx,
            ),
            AddPhoneNumberNode.DEFAULT_NAME,
        )
        self.assertEqual(
            flow.find_next_node(
                WaitEmailVerificationCodeNode.DEFAULT_NAME,
                result=NodeResult.ok(
                    status=WaitEmailVerificationCodeNode.CODEX_CONSENT_STATUS
                ),
                ctx=ctx,
            ),
            SubmitCodexConsentNode.DEFAULT_NAME,
        )
        self.assertEqual(
            flow.find_next_node(
                FillAboutYouNode.DEFAULT_NAME,
                result=NodeResult.ok(status=FillAboutYouNode.SUCCESS_STATUS),
                ctx=ctx,
            ),
            SelectCodexAccountNode.DEFAULT_NAME,
        )
        self.assertEqual(
            flow.find_next_node(
                SelectCodexAccountNode.DEFAULT_NAME,
                result=NodeResult.ok(
                    status=SelectCodexAccountNode.SUCCESS_EMAIL_VERIFICATION_READY_STATUS
                ),
                ctx=ctx,
            ),
            WaitEmailVerificationCodeNode.DEFAULT_NAME,
        )
        self.assertEqual(
            flow.find_next_node(
                SelectCodexAccountNode.DEFAULT_NAME,
                result=NodeResult.ok(
                    status=SelectCodexAccountNode.SUCCESS_NEEDS_PHONE_STATUS
                ),
                ctx=ctx,
            ),
            AddPhoneNumberNode.DEFAULT_NAME,
        )
        self.assertEqual(
            flow.find_next_node(
                SelectCodexAccountNode.DEFAULT_NAME,
                result=NodeResult.ok(status=SelectCodexAccountNode.SUCCESS_CONSENT_STATUS),
                ctx=ctx,
            ),
            SubmitCodexConsentNode.DEFAULT_NAME,
        )
        self.assertEqual(
            flow.find_next_node(
                AddPhoneNumberNode.DEFAULT_NAME,
                result=NodeResult.ok(
                    status=AddPhoneNumberNode.OAUTH_REAUTH_REQUIRED_STATUS
                ),
                ctx=ctx,
            ),
            SelectCodexAccountNode.DEFAULT_NAME,
        )
        self.assertEqual(
            flow.find_next_node(
                AddPhoneNumberNode.DEFAULT_NAME,
                result=NodeResult.ok(status=AddPhoneNumberNode.SUCCESS_STATUS),
                ctx=ctx,
            ),
            WaitSmsVerificationCodeNode.DEFAULT_NAME,
        )
        self.assertEqual(
            flow.find_next_node(
                WaitSmsVerificationCodeNode.DEFAULT_NAME,
                result=NodeResult.ok(
                    status=WaitSmsVerificationCodeNode.RETRY_SELECT_CODEX_ACCOUNT_STATUS
                ),
                ctx=ctx,
            ),
            SelectCodexAccountNode.DEFAULT_NAME,
        )
        self.assertEqual(
            flow.find_next_node(
                WaitSmsVerificationCodeNode.DEFAULT_NAME,
                result=NodeResult.ok(status=WaitSmsVerificationCodeNode.SUCCESS_STATUS),
                ctx=ctx,
            ),
            SubmitCodexConsentNode.DEFAULT_NAME,
        )


if __name__ == "__main__":
    unittest.main()
