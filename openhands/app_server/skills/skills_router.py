from fastapi import APIRouter

from openhands.app_server.skills.skill_import import skill_import_router
from openhands.app_server.skills.skill_install import skill_install_router
from openhands.app_server.skills.skill_repo import skill_repo_router

router = APIRouter(tags=['Skills'])
router.include_router(skill_repo_router.router)
router.include_router(skill_install_router.router)
router.include_router(skill_import_router.router)
