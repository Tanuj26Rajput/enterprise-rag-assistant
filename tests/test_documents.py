"""Document upload, validation and the ingestion pipeline."""

from __future__ import annotations

import io
import uuid
import zipfile

import pytest
from httpx import AsyncClient

from tests.conftest import add_member, make_collection, make_namespace, signed_in
from tests.fakes import FakeStore

LOREM = (
    "Retrieval augmented generation grounds a language model in retrieved "
    "evidence so that its answers can be attributed to a source document. "
)


# ------------------------------------------------------------- file builders ---
def make_pdf(pages: list[str]) -> bytes:
    import pymupdf

    document = pymupdf.open()
    for text in pages:
        page = document.new_page()
        page.insert_text((72, 72), text, fontsize=11)
    data = document.tobytes()
    document.close()
    return data


def make_docx(paragraphs: list[str]) -> bytes:
    import docx

    document = docx.Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.fixture
async def owner(client: AsyncClient) -> dict[str, str]:
    return await signed_in(client, "owner@example.com")


@pytest.fixture
async def namespace(client: AsyncClient, owner: dict[str, str]) -> dict:
    return await make_namespace(client, owner, "acme")


@pytest.fixture
async def collection(client: AsyncClient, owner: dict, namespace: dict) -> dict:
    return await make_collection(client, owner, namespace["id"], "policies")


def upload_url(namespace_id: str, collection_id: str) -> str:
    return f"/api/v1/namespaces/{namespace_id}/collections/{collection_id}/documents"


async def upload(
    client: AsyncClient,
    headers: dict[str, str],
    namespace_id: str,
    collection_id: str,
    *,
    filename: str,
    content: bytes,
    content_type: str = "application/octet-stream",
    **data: str,
):
    return await client.post(
        upload_url(namespace_id, collection_id),
        headers=headers,
        files={"file": (filename, content, content_type)},
        data=data,
    )


class TestSuccessfulUpload:
    async def test_plain_text_is_ingested_into_chunks(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        response = await upload(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="notes.txt",
            content=(LOREM * 40).encode(),
            content_type="text/plain",
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "ready"
        assert body["mime_type"] == "text/plain"
        assert body["chunk_count"] > 1
        assert body["token_count"] > 0
        assert body["error_message"] is None

    async def test_the_storage_uri_is_never_exposed(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        response = await upload(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="notes.txt",
            content=(LOREM * 5).encode(),
            content_type="text/plain",
        )

        assert "storage_uri" not in response.text

    async def test_a_pdf_keeps_its_page_numbers(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        pdf = make_pdf([f"Page {n} content about retrieval policies." for n in range(1, 4)])

        response = await upload(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="handbook.pdf",
            content=pdf,
            content_type="application/pdf",
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["page_count"] == 3

        chunks = (
            await client.get(
                f"/api/v1/namespaces/{namespace['id']}/documents/{body['id']}/chunks",
                headers=owner,
            )
        ).json()["items"]
        assert chunks
        assert all(chunk["page_number"] is not None for chunk in chunks)
        assert {chunk["page_number"] for chunk in chunks} <= {1, 2, 3}

    async def test_a_docx_is_ingested_without_page_numbers(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        """DOCX has no page concept until it is laid out; inventing one would
        produce citations the reader cannot follow."""
        docx_bytes = make_docx([LOREM * 3, LOREM * 3])

        response = await upload(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="policy.docx",
            content=docx_bytes,
            content_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "ready"
        assert body["page_count"] is None

        chunks = (
            await client.get(
                f"/api/v1/namespaces/{namespace['id']}/documents/{body['id']}/chunks",
                headers=owner,
            )
        ).json()["items"]
        assert all(chunk["page_number"] is None for chunk in chunks)

    async def test_chunk_offsets_point_at_the_real_text(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        source = (LOREM * 30).encode()
        body = (
            await upload(
                client,
                owner,
                namespace["id"],
                collection["id"],
                filename="notes.txt",
                content=source,
                content_type="text/plain",
            )
        ).json()

        chunks = (
            await client.get(
                f"/api/v1/namespaces/{namespace['id']}/documents/{body['id']}/chunks",
                headers=owner,
            )
        ).json()["items"]

        text = source.decode()
        for chunk in chunks:
            assert text[chunk["char_start"] : chunk["char_end"]] == chunk["content"]

    async def test_chunk_indexes_are_contiguous(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        body = (
            await upload(
                client,
                owner,
                namespace["id"],
                collection["id"],
                filename="notes.txt",
                content=(LOREM * 40).encode(),
                content_type="text/plain",
            )
        ).json()

        chunks = (
            await client.get(
                f"/api/v1/namespaces/{namespace['id']}/documents/{body['id']}/chunks?limit=100",
                headers=owner,
            )
        ).json()["items"]

        assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))

    async def test_the_collection_counters_are_updated(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        await upload(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="notes.txt",
            content=(LOREM * 20).encode(),
            content_type="text/plain",
        )

        refreshed = (
            await client.get(
                f"/api/v1/namespaces/{namespace['id']}/collections/{collection['id']}",
                headers=owner,
            )
        ).json()
        assert refreshed["document_count"] == 1
        assert refreshed["chunk_count"] > 0

    async def test_the_upload_is_audited(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict, store: FakeStore
    ) -> None:
        await upload(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="notes.txt",
            content=(LOREM * 5).encode(),
            content_type="text/plain",
        )

        assert len(store.events("document.upload")) == 1
        assert len(store.events("document.process")) == 1


class TestInvalidFiles:
    async def test_an_unsupported_extension_is_rejected(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        response = await upload(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="payload.exe",
            content=b"MZ\x90\x00binary",
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_failed"

    async def test_an_executable_renamed_to_txt_is_rejected(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        """The extension is not evidence; the bytes are."""
        response = await upload(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="payload.txt",
            content=b"MZ\x90\x00\x03\x00\x00\x00\xff\xfe binary payload",
            content_type="text/plain",
        )

        assert response.status_code == 422

    async def test_a_pdf_extension_on_non_pdf_bytes_is_rejected(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        response = await upload(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="fake.pdf",
            content=b"This is plain text pretending to be a PDF.",
            content_type="application/pdf",
        )

        assert response.status_code == 422
        assert "extension" in response.text

    async def test_a_zip_that_is_not_a_docx_is_rejected(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("not-word/data.xml", "<xml/>")

        response = await upload(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="fake.docx",
            content=buffer.getvalue(),
        )

        assert response.status_code == 422
        assert "Word document" in response.text

    async def test_an_empty_file_is_rejected(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        response = await upload(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="empty.txt",
            content=b"",
            content_type="text/plain",
        )

        assert response.status_code == 422
        assert "empty" in response.json()["error"]["message"].lower()

    async def test_a_whitespace_only_document_fails_processing(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        """It is a valid file, so it is stored - but it yields no chunks and
        the document is marked failed with an explanation."""
        response = await upload(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="blank.txt",
            content=b"   \n\n\t   \n",
            content_type="text/plain",
        )

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "failed"
        assert body["chunk_count"] == 0
        assert body["error_message"]

    async def test_a_corrupt_pdf_fails_processing_without_a_traceback(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        response = await upload(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="broken.pdf",
            content=b"%PDF-1.7\nnot actually a valid pdf body",
            content_type="application/pdf",
        )

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "failed"
        assert "Traceback" not in response.text

    async def test_a_filename_is_required(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        response = await upload(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="",
            content=b"hello",
        )

        assert response.status_code == 422

    async def test_a_traversal_filename_is_sanitised(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        response = await upload(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="../../../etc/passwd.txt",
            content=(LOREM * 3).encode(),
            content_type="text/plain",
        )

        assert response.status_code == 201
        assert response.json()["filename"] == "passwd.txt"


class TestUploadLimits:
    async def test_a_file_over_the_limit_is_rejected(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        """The test settings cap uploads at 1 MB."""
        oversized = b"a" * (1024 * 1024 + 1024)

        response = await upload(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="big.txt",
            content=oversized,
            content_type="text/plain",
        )

        assert response.status_code == 422
        assert "size" in response.json()["error"]["message"].lower()

    async def test_a_file_at_the_limit_is_accepted(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        at_limit = (LOREM.encode() * (1024 * 1024 // len(LOREM)))[: 1024 * 1024]

        response = await upload(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="large.txt",
            content=at_limit,
            content_type="text/plain",
        )

        assert response.status_code == 201

    async def test_identical_content_is_rejected_as_a_duplicate(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        content = (LOREM * 10).encode()
        await upload(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="first.txt",
            content=content,
            content_type="text/plain",
        )

        response = await upload(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="second-name-same-bytes.txt",
            content=content,
            content_type="text/plain",
        )

        assert response.status_code == 409
        assert response.json()["error"]["details"]["document_id"]

    async def test_identical_content_is_allowed_in_a_different_collection(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        other = await make_collection(client, owner, namespace["id"], "handbook")
        content = (LOREM * 10).encode()
        await upload(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="doc.txt",
            content=content,
            content_type="text/plain",
        )

        response = await upload(
            client,
            owner,
            namespace["id"],
            other["id"],
            filename="doc.txt",
            content=content,
            content_type="text/plain",
        )

        assert response.status_code == 201


class TestAuthorization:
    @pytest.fixture
    async def viewer(self, client: AsyncClient, owner: dict, namespace: dict) -> dict[str, str]:
        headers = await signed_in(client, "viewer@example.com")
        await add_member(client, owner, namespace["id"], "viewer@example.com", "viewer")
        return headers

    async def test_unauthenticated_upload_is_rejected(
        self, client: AsyncClient, namespace: dict, collection: dict
    ) -> None:
        response = await client.post(
            upload_url(namespace["id"], collection["id"]),
            files={"file": ("notes.txt", b"hello there", "text/plain")},
        )

        assert response.status_code == 401

    async def test_a_viewer_cannot_upload(
        self, client: AsyncClient, namespace: dict, collection: dict, viewer: dict
    ) -> None:
        response = await upload(
            client,
            viewer,
            namespace["id"],
            collection["id"],
            filename="notes.txt",
            content=(LOREM * 3).encode(),
            content_type="text/plain",
        )

        assert response.status_code == 403

    async def test_an_outsider_cannot_upload(
        self, client: AsyncClient, namespace: dict, collection: dict
    ) -> None:
        outsider = await signed_in(client, "outsider@example.com")

        response = await upload(
            client,
            outsider,
            namespace["id"],
            collection["id"],
            filename="notes.txt",
            content=(LOREM * 3).encode(),
            content_type="text/plain",
        )

        assert response.status_code == 404

    async def test_an_outsider_cannot_read_a_document(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        body = (
            await upload(
                client,
                owner,
                namespace["id"],
                collection["id"],
                filename="notes.txt",
                content=(LOREM * 3).encode(),
                content_type="text/plain",
            )
        ).json()
        outsider = await signed_in(client, "outsider@example.com")

        response = await client.get(
            f"/api/v1/namespaces/{namespace['id']}/documents/{body['id']}", headers=outsider
        )

        assert response.status_code == 404

    async def test_a_document_cannot_be_read_through_another_namespace(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        body = (
            await upload(
                client,
                owner,
                namespace["id"],
                collection["id"],
                filename="notes.txt",
                content=(LOREM * 3).encode(),
                content_type="text/plain",
            )
        ).json()
        other = await make_namespace(client, owner, "other-co")

        response = await client.get(
            f"/api/v1/namespaces/{other['id']}/documents/{body['id']}", headers=owner
        )

        assert response.status_code == 404

    async def test_uploading_to_an_unknown_collection_is_not_found(
        self, client: AsyncClient, namespace: dict, owner: dict
    ) -> None:
        response = await upload(
            client,
            owner,
            namespace["id"],
            str(uuid.uuid4()),
            filename="notes.txt",
            content=(LOREM * 3).encode(),
            content_type="text/plain",
        )

        assert response.status_code == 404

    async def test_a_viewer_cannot_delete_a_document(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict, viewer: dict
    ) -> None:
        body = (
            await upload(
                client,
                owner,
                namespace["id"],
                collection["id"],
                filename="notes.txt",
                content=(LOREM * 3).encode(),
                content_type="text/plain",
            )
        ).json()

        response = await client.delete(
            f"/api/v1/namespaces/{namespace['id']}/documents/{body['id']}", headers=viewer
        )

        assert response.status_code == 403

    async def test_a_member_cannot_delete_someone_elses_document(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        other_member = await signed_in(client, "member@example.com")
        await add_member(client, owner, namespace["id"], "member@example.com", "member")
        body = (
            await upload(
                client,
                owner,
                namespace["id"],
                collection["id"],
                filename="notes.txt",
                content=(LOREM * 3).encode(),
                content_type="text/plain",
            )
        ).json()

        response = await client.delete(
            f"/api/v1/namespaces/{namespace['id']}/documents/{body['id']}",
            headers=other_member,
        )

        assert response.status_code == 403

    async def test_an_uploader_can_delete_their_own_document(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict
    ) -> None:
        member = await signed_in(client, "member@example.com")
        await add_member(client, owner, namespace["id"], "member@example.com", "member")
        body = (
            await upload(
                client,
                member,
                namespace["id"],
                collection["id"],
                filename="notes.txt",
                content=(LOREM * 3).encode(),
                content_type="text/plain",
            )
        ).json()

        response = await client.delete(
            f"/api/v1/namespaces/{namespace['id']}/documents/{body['id']}", headers=member
        )

        assert response.status_code == 204


class TestReprocess:
    async def test_reprocessing_replaces_the_chunks(
        self, client: AsyncClient, namespace: dict, collection: dict, owner: dict, store: FakeStore
    ) -> None:
        body = (
            await upload(
                client,
                owner,
                namespace["id"],
                collection["id"],
                filename="notes.txt",
                content=(LOREM * 20).encode(),
                content_type="text/plain",
            )
        ).json()
        original_ids = {str(c.id) for c in store.chunks.values()}

        response = await client.post(
            f"/api/v1/namespaces/{namespace['id']}/documents/{body['id']}/reprocess",
            headers=owner,
        )

        assert response.status_code == 200
        assert response.json()["status"] == "ready"
        assert original_ids.isdisjoint({str(c.id) for c in store.chunks.values()})
        assert response.json()["chunk_count"] == body["chunk_count"]
