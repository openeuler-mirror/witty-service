from fastapi import APIRouter

from openhands.app_server.skills.skill_repo import skill_repo_router

router = APIRouter(tags=['Skills'])
router.include_router(skill_repo_router.router)
