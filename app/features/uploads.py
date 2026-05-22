import uuid
from fastapi import UploadFile, HTTPException
from app.config import supabase, SUPABASE_BUCKET


async def upload_medical_document(user, file: UploadFile):
    """
    Uploads a medical document to Supabase Storage and returns its public URL.
    Stored under: hrms-docs/medical_docs/<user_id>/<uuid>.<ext>
    """
    try:
        # --- Verify bucket existence ---
        try:
            buckets = supabase.storage.list_buckets()
            bucket_names = [b.name for b in buckets]  # ✅ updated for SDK v2.x
            if SUPABASE_BUCKET not in bucket_names:
                raise HTTPException(
                    status_code=404,
                    detail=f"Bucket '{SUPABASE_BUCKET}' not found in Supabase."
                )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Could not verify Supabase bucket: {str(e)}")

        # --- Build file path ---
        ext = file.filename.split('.')[-1]
        file_path = f"medical_docs/{user['id']}/{uuid.uuid4()}.{ext}"

        # --- Read file bytes ---
        file_bytes = await file.read()

        # --- Upload to Supabase ---
        upload_res = supabase.storage.from_(SUPABASE_BUCKET).upload(file_path, file_bytes)

        # SDK v2.x returns a `SyncResponse` object, so check `.error` attribute
        if hasattr(upload_res, "error") and upload_res.error:
            raise HTTPException(status_code=500, detail=f"Upload failed: {upload_res.error.message}")

        # --- Get public URL ---
        public_url = supabase.storage.from_(SUPABASE_BUCKET).get_public_url(file_path)

        return {
            "status": "success",
            "file_url": public_url,
            "path": file_path
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")
