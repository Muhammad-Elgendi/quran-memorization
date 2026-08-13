from fastapi import APIRouter, HTTPException

from ..services.quran_service import QuranService


def create_router(quran_service: QuranService) -> APIRouter:
    router = APIRouter(prefix="/api/quran", tags=["Quran"])

    @router.get("/surahs")
    def list_surahs():
        return quran_service.get_surahs()

    @router.get("/surahs/{surah_number}")
    def get_surah(surah_number: int):
        surah = quran_service.get_surah(surah_number)
        if not surah:
            raise HTTPException(status_code=404, detail="Surah not found")
        return surah

    @router.get("/surahs/{surah_number}/ayahs/{ayah_number}")
    def get_ayah(surah_number: int, ayah_number: int):
        ayah = quran_service.get_ayah(surah_number, ayah_number)
        if not ayah:
            raise HTTPException(status_code=404, detail="Ayah not found")
        return ayah

    return router
