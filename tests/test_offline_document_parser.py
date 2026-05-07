import io
import unittest
import zipfile
from pathlib import Path

from app.services.offline_document_parser import OfflineDocumentParser


class OfflineDocumentParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = OfflineDocumentParser()

    def test_parse_markdown_extracts_headings_and_paragraphs(self) -> None:
        content = b"# Background\n\nThis is the first paragraph.\n\n## Next Steps\n\n- Finish the API\n"

        result = self.parser.parse_bytes(
            file_name="offline-note.md",
            content=content,
            content_type="text/markdown",
        )

        self.assertEqual(result.file_extension, ".md")
        self.assertEqual(result.section_count, 2)
        self.assertEqual(result.headings, ["Background", "Next Steps"])
        self.assertEqual(result.package["sections"][0]["paragraphs"][0], "This is the first paragraph.")

    def test_parse_text_groups_paragraphs(self) -> None:
        content = "First paragraph\n\nSecond paragraph".encode("utf-8")

        result = self.parser.parse_bytes(
            file_name="meeting.txt",
            content=content,
            content_type="text/plain",
        )

        self.assertEqual(result.file_extension, ".txt")
        self.assertEqual(result.section_count, 1)
        self.assertEqual(result.paragraph_count, 2)
        self.assertEqual(len(result.package["sections"][0]["paragraphs"]), 2)

    def test_parse_markdown_preserves_table_rows_as_structured_blocks(self) -> None:
        content = (
            "# Review Notes\n\n"
            "| Module | Status |\n"
            "| --- | --- |\n"
            "| Canvas | Ready |\n"
            "| PPT | Syncing |\n"
        ).encode("utf-8")

        result = self.parser.parse_bytes(
            file_name="offline-table.md",
            content=content,
            content_type="text/markdown",
        )

        self.assertEqual(result.section_count, 1)
        table_block = result.package["sections"][0]["paragraphs"][0]
        self.assertEqual(table_block["type"], "table")
        self.assertEqual(table_block["rows"][0], ["Module", "Status"])
        self.assertEqual(table_block["rows"][1], ["Canvas", "Ready"])
        self.assertEqual(table_block["rows"][2], ["PPT", "Syncing"])

    def test_parse_docx_reads_headings_and_extracts_tables_and_images(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "[Content_Types].xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
</Types>""",
            )
            archive.writestr(
                "word/document.xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<w:document
  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
  xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
  xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Review Notes</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:t>Update the workbench first.</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Module</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Status</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Canvas</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Ready</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:p>
      <w:r>
        <w:drawing>
          <wp:inline>
            <wp:extent cx="952500" cy="476250"/>
            <wp:docPr id="1" name="Architecture" descr="Architecture Flow"/>
            <a:graphic>
              <a:graphicData>
                <a:blip r:embed="rIdImage1"/>
              </a:graphicData>
            </a:graphic>
          </wp:inline>
        </w:drawing>
      </w:r>
    </w:p>
  </w:body>
</w:document>""",
            )
            archive.writestr(
                "word/_rels/document.xml.rels",
                """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship
    Id="rIdImage1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
    Target="media/image1.png"/>
</Relationships>""",
            )
            archive.writestr("word/media/image1.png", b"fake-png")

        result = self.parser.parse_bytes(
            file_name="review.docx",
            content=buffer.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        self.assertEqual(result.file_extension, ".docx")
        self.assertEqual(result.section_count, 1)
        self.assertEqual(result.headings, ["Review Notes"])
        table_block = result.package["sections"][0]["paragraphs"][1]
        self.assertEqual(table_block["type"], "table")
        self.assertEqual(table_block["rows"][0], ["Module", "Status"])
        self.assertEqual(table_block["rows"][1], ["Canvas", "Ready"])
        image_block = result.package["sections"][0]["paragraphs"][2]
        self.assertEqual(image_block["type"], "image")
        self.assertEqual(image_block["caption"], "Architecture Flow")
        self.assertEqual(image_block["width"], 100)
        self.assertEqual(image_block["height"], 50)
        self.assertTrue(Path(image_block["local_path"]).is_file())
        self.assertEqual(len(result.package["assets"]), 1)
        self.assertEqual(result.package["assets"][0]["kind"], "image")

    def test_parse_bytes_rejects_unsupported_extension(self) -> None:
        with self.assertRaisesRegex(ValueError, "docx / md / txt"):
            self.parser.parse_bytes(
                file_name="slides.pdf",
                content=b"fake",
                content_type="application/pdf",
            )


if __name__ == "__main__":
    unittest.main()
