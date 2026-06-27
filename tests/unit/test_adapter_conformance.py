from localize.formats import JSON_ADAPTER, JAVA_PROPERTIES_ADAPTER
from localize.formats.testing import (
    LocalizationAdapterConformanceCase,
    assert_localization_adapter_conformance,
)


def test_java_properties_adapter_satisfies_conformance_contract():
    assert_localization_adapter_conformance(
        JAVA_PROPERTIES_ADAPTER,
        LocalizationAdapterConformanceCase(
            source_content="hello=Hello {0}\nbye=Bye\n",
            target_content="hello=Hallo {0}\n",
            expected_source_translations={
                "hello": "Hello {0}",
                "bye": "Bye",
            },
            expected_target_translations={"hello": "Hallo {0}"},
            expected_added_keys={"bye"},
            changed_diff_line="hello=Hallo {0}",
            expected_changed_key="hello",
            review_keys=("hello",),
            expected_review_fragment="hello=Hallo {0}",
            escape_source_value="Hello {0}",
            escape_translation_value="L'utilisateur {0}",
            expected_escaped_translation="L''utilisateur {0}",
        ),
    )


def test_json_adapter_satisfies_conformance_contract():
    assert_localization_adapter_conformance(
        JSON_ADAPTER,
        LocalizationAdapterConformanceCase(
            source_content='{"title": "Hello", "nested": {"cta": "Start {0}"}}\n',
            target_content='{"title": "Hallo"}\n',
            expected_source_translations={
                "/title": "Hello",
                "/nested/cta": "Start {0}",
            },
            expected_target_translations={"/title": "Hallo"},
            expected_added_keys={"/nested/cta"},
            changed_diff_line='  "title": "Hallo"',
            expected_changed_key="/title",
            review_keys=("/title",),
            expected_review_fragment='"/title": "Hallo"',
            escape_source_value="Start {0}",
            escape_translation_value="L'utilisateur {0}",
            expected_escaped_translation="L'utilisateur {0}",
        ),
    )
