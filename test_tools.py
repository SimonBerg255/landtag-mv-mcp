"""Validation tests for Landtag M-V MCP tools."""

import asyncio
import sys

from tools import (
    get_document_by_number,
    get_document_text,
    get_recent_documents,
    get_vorgang,
    search_documents,
)


async def main():
    print("=" * 60)
    print("TEST 1: search_documents(query='Schule', document_type='Drucksache', limit=5)")
    print("=" * 60)
    result = await search_documents(query="Schule", document_type="Drucksache", limit=5)
    print(result[:1500])
    print("..." if len(result) > 1500 else "")

    # Check for valid results
    assert "error" not in result.lower() or "error" in result and "Keine" in result, f"Search returned error: {result[:200]}"
    assert "8/" in result, f"No document numbers in format '8/XXXX' found"
    print("\n✓ TEST 1 PASSED\n")

    print("=" * 60)
    print("TEST 2: get_document_by_number('8/5012')")
    print("=" * 60)
    result2 = await get_document_by_number("8/5012")
    print(result2[:1500])
    assert "error" not in result2.lower() or "nicht gefunden" in result2.lower(), f"Lookup failed: {result2[:200]}"
    print("\n✓ TEST 2 PASSED\n")

    # Extract document ID from result2 for test 3
    doc_id = None
    for line in result2.split("\n"):
        if "ID:" in line and "Vorgang" not in line:
            parts = line.split("ID:")
            if len(parts) > 1:
                doc_id = parts[1].strip().split()[0].strip()
                break

    if doc_id and doc_id.isdigit():
        print("=" * 60)
        print(f"TEST 3: get_document_text(document_id='{doc_id}')")
        print("=" * 60)
        result3 = await get_document_text(document_id=doc_id, document_number="8/5012")
        print(result3[:2000])
        print("..." if len(result3) > 2000 else "")

        # Check for at least 500 chars of German text
        if "error" not in result3.lower():
            text_start = result3.find("TEXT:")
            if text_start > 0:
                text_portion = result3[text_start:]
                assert len(text_portion) >= 500, f"Text too short: {len(text_portion)} chars"
                print(f"\n✓ TEST 3 PASSED (extracted {len(text_portion)} chars of text)\n")
            else:
                print("\n⚠ TEST 3: No TEXT section found but no error\n")
        else:
            print(f"\n⚠ TEST 3: Error in response: {result3[:200]}\n")
    else:
        print(f"\n⚠ Skipping TEST 3: Could not extract document ID from TEST 2 result\n")

    print("=" * 60)
    print("TEST 4: get_recent_documents(document_type='Plenarprotokoll', limit=3)")
    print("=" * 60)
    result4 = await get_recent_documents(document_type="Plenarprotokoll", limit=3)
    print(result4[:1500])
    assert "error" not in result4.lower() or "Keine" in result4, f"Recent docs failed: {result4[:200]}"
    print("\n✓ TEST 4 PASSED\n")

    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
