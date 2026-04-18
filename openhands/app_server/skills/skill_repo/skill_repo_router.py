from fastapi import APIRouter, BackgroundTasks, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.app_server.config import depends_db_session, depends_user_context
from openhands.app_server.skills.models.skill_discovery_models import SkillDiscoveryItem
from openhands.app_server.skills.skill_repo.skill_repo_models import (
    CreateSkillRepoRequest,
    SkillRepo,
    SkillRepoPage,
    UpdateSkillRepoRequest,
)
from openhands.app_server.skills.skill_repo.skill_repo_service import SkillRepoService
from openhands.app_server.user.user_context import UserContext

router = APIRouter(tags=['Skills'])
user_dependency = depends_user_context()
db_session_dependency = depends_db_session()


class SkillRepoDiscoverPage(BaseModel):
    items: list[SkillDiscoveryItem]


class SkillRepoDiscoverStatusItem(BaseModel):
    repo_id: str
    repo_name: str
    discover_status: str
    skill_num: int


class SkillRepoDiscoverStatus(BaseModel):
    items: list[SkillRepoDiscoverStatusItem]


def _build_service(
    db_session: AsyncSession, user_context: UserContext
) -> SkillRepoService:
    return SkillRepoService(db_session=db_session, user_context=user_context)


@router.get('/skills/repos', response_model=SkillRepoPage)
async def list_skill_repos(
    user_context: UserContext = user_dependency,
    db_session: AsyncSession = db_session_dependency,
) -> SkillRepoPage:
    service = _build_service(db_session, user_context)
    try:
        items = await service.list_skill_repos()
    except PermissionError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    return SkillRepoPage(items=items)


@router.post(
    '/skills/repos', response_model=SkillRepo, status_code=status.HTTP_201_CREATED
)
async def create_skill_repo(
    request: CreateSkillRepoRequest,
    background_tasks: BackgroundTasks,
    user_context: UserContext = user_dependency,
    db_session: AsyncSession = db_session_dependency,
) -> SkillRepo:
    service = _build_service(db_session, user_context)
    try:
        created_skillrepo = await service.create_skill_repo(request)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    user_id = await user_context.get_user_id() or 'anonymous'
    # Trigger discovery in the background without blocking the response.
    background_tasks.add_task(
        SkillRepoService.discover_repo_in_background,
        repo_id=created_skillrepo.repo_id,
        user_id=user_id,
    )
    return created_skillrepo


@router.patch('/skills/repos/{repo_id}', response_model=SkillRepo)
async def update_skill_repo(
    repo_id: str,
    request: UpdateSkillRepoRequest,
    user_context: UserContext = user_dependency,
    db_session: AsyncSession = db_session_dependency,
) -> SkillRepo:
    service = _build_service(db_session, user_context)
    try:
        return await service.update_skill_repo(repo_id, request)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.delete('/skills/repos/{repo_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill_repo(
    repo_id: str,
    user_context: UserContext = user_dependency,
    db_session: AsyncSession = db_session_dependency,
) -> Response:
    service = _build_service(db_session, user_context)
    try:
        await service.delete_skill_repo(repo_id)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post('/skills/repos/discover', response_model=SkillRepoDiscoverPage)
async def discover_repos_skill(
    user_context: UserContext = user_dependency,
    db_session: AsyncSession = db_session_dependency,
) -> SkillRepoDiscoverPage:
    service = _build_service(db_session, user_context)
    try:
        items = await service.discover_repos_skill()
    except PermissionError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    return SkillRepoDiscoverPage(items=items)


@router.post('/skills/repos/discover/{repo_id}', response_model=SkillRepoDiscoverPage)
async def discover_one_repo_skill(
    repo_id: str,
    user_context: UserContext = user_dependency,
    db_session: AsyncSession = db_session_dependency,
) -> SkillRepoDiscoverPage:
    service = _build_service(db_session, user_context)
    try:
        items = await service.discover_one_repo_skill(repo_id)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        if str(exc) == 'Skill repo discovery is already in progress':
            raise HTTPException(status.HTTP_202_ACCEPTED, detail=str(exc)) from exc
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return SkillRepoDiscoverPage(items=items)


@router.get('/skills/repos/discover', response_model=SkillRepoDiscoverPage)
async def get_discovered_repos_skill(
    user_context: UserContext = user_dependency,
    db_session: AsyncSession = db_session_dependency,
) -> SkillRepoDiscoverPage:
    service = _build_service(db_session, user_context)
    try:
        items = await service.get_discovered_repos_skill()
    except PermissionError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    return SkillRepoDiscoverPage(items=items)


@router.get('/skills/repos/discover/{repo_id}', response_model=SkillRepoDiscoverPage)
async def get_discovered_one_repo_skill(
    repo_id: str,
    user_context: UserContext = user_dependency,
    db_session: AsyncSession = db_session_dependency,
) -> SkillRepoDiscoverPage:
    service = _build_service(db_session, user_context)
    try:
        items = await service.get_discovered_one_repo_skill(repo_id)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return SkillRepoDiscoverPage(items=items)


@router.get('/skills/repos/discover-status', response_model=SkillRepoDiscoverStatus)
async def get_discover_status(
    user_context: UserContext = user_dependency,
    db_session: AsyncSession = db_session_dependency,
) -> SkillRepoDiscoverStatus:
    service = _build_service(db_session, user_context)
    try:
        raw_items = await service.get_discover_status()
    except PermissionError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    items = [
        SkillRepoDiscoverStatusItem(
            repo_id=item['repo_id'],
            repo_name=item['repo_name'],
            discover_status=item['discover_status'],
            skill_num=item['skill_num'],
        )
        for item in raw_items
    ]
    return SkillRepoDiscoverStatus(items=items)
