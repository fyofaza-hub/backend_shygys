from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal, TypeVar
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    HttpUrl,
    StringConstraints,
    model_validator,
)

Locale = Literal["kk", "ru", "en"]
Accuracy = Literal["exact", "locality", "district"]
Slug = Annotated[str, StringConstraints(min_length=2, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=30000)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
ConsentType = Literal[
    "data_processing",
    "public_contacts",
    "portfolio",
    "marketing",
    "gis_export",
    "ai_translation",
    "ai_search",
]


class Role(StrEnum):
    guest = "guest"
    creator = "creator"
    org_rep = "org_rep"
    moderator = "moderator"
    content_editor = "content_editor"
    analyst = "analyst"
    center_curator = "center_curator"
    admin = "admin"
    superadmin = "superadmin"


class LegalStatus(StrEnum):
    individual = "individual"
    sole_proprietor = "sole_proprietor"
    legal_entity = "legal_entity"


class Residency(StrEnum):
    none = "none"
    pending = "pending"
    active = "active"
    expired = "expired"
    revoked = "revoked"


class Moderation(StrEnum):
    draft = "draft"
    pending_review = "pending_review"
    changes_requested = "changes_requested"
    approved = "approved"
    rejected = "rejected"
    suspended = "suspended"


class Publication(StrEnum):
    unpublished = "unpublished"
    published = "published"
    hidden = "hidden"
    archived = "archived"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Localized(Model):
    kk: Text
    ru: Text
    en: Text

    def complete(self) -> bool:
        return all((self.kk, self.ru, self.en))


def empty_text() -> Localized:
    return Localized(kk="", ru="", en="")


class PageQuery(Model):
    page: int = Field(1, ge=1, le=100000)
    limit: int = Field(20, ge=1, le=100)


T = TypeVar("T")


class Page[T](Model):
    items: list[T]
    total: int
    page: int
    limit: int


class CatalogFilters(PageQuery):
    category_id: list[UUID] = Field(default_factory=list, max_length=30)
    oked_code: list[str] = Field(default_factory=list, max_length=43)
    is_resident: bool | None = None
    open_to_collab: bool | None = None
    q: str | None = Field(None, min_length=1, max_length=200)
    category: list[UUID] = Field(default_factory=list, max_length=30)
    oked: list[str] = Field(default_factory=list, max_length=43)
    district_id: UUID | None = None
    locality_id: UUID | None = None
    legal_status: LegalStatus | None = None
    residency: Residency | None = None
    available_for_collaboration: bool | None = None


class MapFilters(CatalogFilters):
    bbox: str = Field(pattern=r"^-?\d+(?:\.\d+)?(?:,-?\d+(?:\.\d+)?){3}$", max_length=120)
    zoom: int = Field(ge=0, le=22)


class Credentials(Model):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128, repr=False)


class Registration(Credentials):
    locale: Locale = "kk"
    policy_version: str = Field(min_length=1, max_length=64)


class Login(Model):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128, repr=False)


class Email(Model):
    email: EmailStr


class EmailToken(Model):
    token: str = Field(min_length=40, max_length=200, repr=False)


class Reset(EmailToken):
    password: str = Field(min_length=12, max_length=128, repr=False)


class AccessToken(Model):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class User(Model):
    id: UUID
    email: EmailStr
    locale: Locale
    roles: list[Role]


class LocaleInput(Model):
    locale: Locale


class ConsentInput(Model):
    type: ConsentType
    policy_version: str = Field(min_length=1, max_length=64)


ProfileTheme = Literal["default", "light", "dark"]
ProfileBlock = Literal["about", "skills", "services", "portfolio", "projects", "contacts"]


class ProfileInput(Model):
    accuracy: Accuracy | None = None
    public_name: ShortText = ""
    legal_status: LegalStatus = LegalStatus.individual
    description: Localized = Field(default_factory=empty_text)
    skills: Localized = Field(default_factory=empty_text)
    experience: Localized = Field(default_factory=empty_text)
    services: Localized = Field(default_factory=empty_text)
    district_id: UUID | None = None
    locality_id: UUID | None = None
    place_id: UUID | None = None
    category_ids: list[UUID] = Field(default_factory=list, max_length=30)
    oked_ids: list[UUID] = Field(default_factory=list, max_length=43)
    avatar_id: UUID | None = None
    cover_id: UUID | None = None
    theme: ProfileTheme = "default"
    block_order: list[ProfileBlock] = Field(default_factory=list, max_length=6)
    links: list[HttpUrl] = Field(default_factory=list, max_length=12)
    available_for_collaboration: bool = False
    work_mode: Literal["offline", "remote", "hybrid"] | None = None
    organization_name: ShortText = ""

    @model_validator(mode="after")
    def unique_profile_blocks(self):
        if len(set(self.block_order)) != len(self.block_order):
            raise ValueError("profile blocks must be unique")
        return self


class ProfileCreate(ProfileInput):
    slug: Slug


class Contacts(Model):
    phone: str | None = Field(None, pattern=r"^\+[1-9][0-9]{7,14}$")
    email: EmailStr | None = None
    phone_public: bool = False
    email_public: bool = False


class LocalizedPatch(Model):
    kk: Text | None = None
    ru: Text | None = None
    en: Text | None = None


class ContactsPatch(Model):
    phone: str | None = Field(None, pattern=r"^\+[1-9][0-9]{7,14}$")
    email: EmailStr | None = None
    phone_public: bool | None = None
    email_public: bool | None = None


class ProfilePatch(Model):
    public_name: ShortText | None = None
    legal_status: LegalStatus | None = None
    description: LocalizedPatch | None = None
    skills: LocalizedPatch | None = None
    experience: LocalizedPatch | None = None
    services: LocalizedPatch | None = None
    district_id: UUID | None = None
    locality_id: UUID | None = None
    place_id: UUID | None = None
    accuracy: Accuracy | None = None
    category_ids: list[UUID] | None = Field(None, max_length=30)
    oked_ids: list[UUID] | None = Field(None, max_length=43)
    avatar_id: UUID | None = None
    cover_id: UUID | None = None
    theme: ProfileTheme | None = None
    block_order: list[ProfileBlock] | None = Field(None, max_length=6)
    links: list[HttpUrl] | None = Field(None, max_length=12)
    available_for_collaboration: bool | None = None
    work_mode: Literal["offline", "remote", "hybrid"] | None = None
    organization_name: ShortText | None = None
    contacts: ContactsPatch | None = None


class Location(Model):
    longitude: float
    latitude: float
    accuracy: Accuracy


class PublicCreator(ProfileInput):
    id: UUID
    slug: Slug
    legal_verified: bool
    residency: Residency
    moderation_status: Moderation
    publication_status: Publication
    phone: str | None
    email: EmailStr | None
    location: Location | None


class GalleryImage(Model):
    media_id: UUID
    sort_order: int = Field(ge=0, le=10000)


class WorkInput(Model):
    title: Localized
    description: Localized = Field(default_factory=empty_text)
    category_id: UUID | None = None
    work_date: date | None = None
    cover_id: UUID | None = None
    media_ids: list[UUID] = Field(default_factory=list, max_length=30)
    video_urls: list[HttpUrl] = Field(default_factory=list, max_length=10)
    author: ShortText = ""
    license: ShortText = ""
    sort_order: int = Field(0, ge=0, le=10000)
    images: list[GalleryImage] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def normalize_gallery(self):
        if self.images:
            self.images.sort(key=lambda image: image.sort_order)
            ids = [image.media_id for image in self.images]
            if len({image.sort_order for image in self.images}) != len(self.images):
                raise ValueError("image sort_order values must be unique")
            if self.media_ids and ids != self.media_ids:
                raise ValueError("images and media_ids must describe the same ordered gallery")
            self.media_ids = ids
        else:
            self.images = [GalleryImage(media_id=id_, sort_order=n) for n, id_ in enumerate(self.media_ids)]
        if len(set(self.media_ids)) != len(self.media_ids):
            raise ValueError("gallery media must be unique")
        return self


class ProjectInput(WorkInput):
    organization_id: UUID | None = None
    creator_ids: list[UUID] = Field(default_factory=list, max_length=50)
    starts_on: date | None = None
    ends_on: date | None = None

    @model_validator(mode="after")
    def valid_dates(self):
        if self.starts_on and self.ends_on and self.starts_on > self.ends_on:
            raise ValueError("ends_on must be on or after starts_on")
        return self


class ProjectCreate(ProjectInput):
    slug: Slug | None = None

    @model_validator(mode="after")
    def slug_is_not_legacy_id(self):
        if self.slug is not None:
            try:
                UUID(self.slug)
            except ValueError:
                return self
            raise ValueError("project slug must not be a UUID")
        return self


class WorkPatch(Model):
    title: LocalizedPatch | None = None
    description: LocalizedPatch | None = None
    category_id: UUID | None = None
    work_date: date | None = None
    cover_id: UUID | None = None
    media_ids: list[UUID] | None = Field(None, max_length=30)
    images: list[GalleryImage] | None = Field(None, max_length=30)
    video_urls: list[HttpUrl] | None = Field(None, max_length=10)
    author: ShortText | None = None
    license: ShortText | None = None
    sort_order: int | None = Field(None, ge=0, le=10000)


class ProjectPatch(WorkPatch):
    organization_id: UUID | None = None
    creator_ids: list[UUID] | None = Field(None, max_length=50)
    starts_on: date | None = None
    ends_on: date | None = None


class PublicWork(WorkInput):
    id: UUID
    creator_id: UUID
    moderation_status: Moderation
    publication_status: Publication


class PublicProject(ProjectInput):
    slug: Slug
    id: UUID
    owner_creator_id: UUID
    moderation_status: Moderation
    publication_status: Publication


class ItemFilters(PageQuery):
    sort: Literal["newest", "popular", "author"] = "newest"
    creator_id: UUID | None = None
    category_id: UUID | None = None
    q: str | None = Field(None, min_length=1, max_length=200)


class Version(Model):
    id: UUID
    creator_id: UUID
    portfolio_item_id: UUID | None
    project_id: UUID | None
    project_slug: Slug | None = None
    payload: ProfileInput | ProjectInput | WorkInput
    status: Moderation
    reason: str | None
    created_at: datetime


class CreatorPage(PublicCreator):
    pinned_works: list[PublicWork]
    projects: Page[PublicProject]


class PortfolioPins(Model):
    work_ids: list[UUID] = Field(max_length=3)

    @model_validator(mode="after")
    def unique_work_ids(self):
        if len(set(self.work_ids)) != len(self.work_ids):
            raise ValueError("pinned works must be unique")
        return self


class ProjectInvitation(Model):
    project_id: UUID
    slug: Slug
    title: Localized
    confirmed_at: datetime | None


class CreatorEditorView(Version):
    slug: Slug
    contacts: Contacts


class Decision(Model):
    decision: Literal["approved", "changes_requested", "rejected"]
    reason: Reason | None = None

    @model_validator(mode="after")
    def reason_required(self):
        if self.decision != "approved" and self.reason is None:
            raise ValueError("reason is required")
        return self


class ModerationFilters(PageQuery):
    decision: Moderation | None = None
    kind: Literal["creator", "portfolio_item", "project"] | None = None
    assigned_to: UUID | None = None
    author_id: UUID | None = None
    creator_id: UUID | None = None
    district_id: UUID | None = None
    reason: str | None = Field(None, max_length=200)
    since: datetime | None = None
    until: datetime | None = None


class Change(Model):
    before: object
    after: object


class Case(Model):
    id: UUID
    version: Version
    diff: dict[str, Change]
    assigned_to: UUID | None
    reviewer_id: UUID | None
    decision: Moderation
    reason: str | None
    created_at: datetime
    decided_at: datetime | None
    possible_duplicates: list[UUID] = Field(default_factory=list)


class Assignment(Model):
    user_id: UUID | None


class Id(Model):
    id: UUID


class ReasonInput(Model):
    reason: Reason


class Marker(Model):
    id: str
    longitude: float
    latitude: float
    count: int
    creator_id: UUID | None
    slug: str | None
    accuracy: Accuracy | None


class MapResults(Model):
    items: list[Marker]
    total: int
    page: int
    limit: int
    zoom: int

    empty_reason: Literal["no_matching_markers"] | None = None


class Reference(Model):
    id: UUID
    code: str
    labels: Localized


class Oked(Reference):
    version: str


class ContentFields(Model):
    title: Localized
    summary: Localized = Field(default_factory=empty_text)
    body: Localized = Field(default_factory=empty_text)
    seo_description: Localized = Field(default_factory=empty_text)


class ArticleImage(Model):
    kind: Literal["image"]
    media_id: UUID


class ArticleVideo(Model):
    kind: Literal["video"]
    url: HttpUrl


class ArticleInput(Model):
    cover_id: UUID | None = None
    meta_tags: Localized = Field(default_factory=empty_text)
    type: Literal["news", "article", "story", "page"] = "news"
    source_locale: Locale
    fields: ContentFields
    media_ids: list[UUID] = Field(default_factory=list, max_length=10)

    gallery: list[Annotated[ArticleImage | ArticleVideo, Field(discriminator="kind")]] = Field(
        default_factory=list, max_length=10
    )

    @model_validator(mode="after")
    def ordered_gallery(self):
        if self.gallery:
            ids = [item.media_id for item in self.gallery if isinstance(item, ArticleImage)]
            if self.media_ids and ids != self.media_ids:
                raise ValueError("gallery and media_ids must contain the same ordered images")
            self.media_ids = ids
        else:
            self.gallery = [ArticleImage(kind="image", media_id=item) for item in self.media_ids]
        if len(set(self.media_ids)) != len(self.media_ids):
            raise ValueError("gallery image IDs must be unique")
        return self


class ArticleCreate(ArticleInput):
    slug: Slug


class Article(ArticleInput):
    id: UUID
    slug: Slug
    status: Publication
    published_at: datetime | None


class Publish(Model):
    override_reason: Reason | None = None
    publish_at: AwareDatetime | None = None


class ArticleFilters(PageQuery):
    type: Literal["news", "article", "story", "page"] | None = None
    q: str | None = Field(None, min_length=1, max_length=200)


class EventInput(Model):
    category_id: UUID | None = None
    fields: ContentFields
    organizer: Localized = Field(default_factory=empty_text)
    starts_at: datetime
    ends_at: datetime
    place_id: UUID | None = None
    registration_url: HttpUrl | None = None

    @model_validator(mode="after")
    def valid_dates(self):
        if not self.starts_at.utcoffset() and self.starts_at.tzinfo is None:
            raise ValueError("starts_at requires a timezone")
        if self.ends_at.tzinfo is None or self.ends_at < self.starts_at:
            raise ValueError("ends_at must specify a timezone and follow starts_at")
        return self


class Event(EventInput):
    publish_at: datetime | None = None
    id: UUID
    status: Publication


class OpportunityInput(Model):
    fields: ContentFields
    kind: Literal["grant", "contest", "accelerator", "program"] = "program"
    source_url: HttpUrl | None = None
    district_id: UUID | None = None
    category_id: UUID | None = None
    legal_status: LegalStatus | None = None
    eligibility: Localized
    deadline: datetime | None = None
    url: HttpUrl | None = None


class Opportunity(OpportunityInput):
    publish_at: datetime | None = None
    id: UUID
    status: Publication


class TextSearch(PageQuery):
    q: str | None = Field(None, min_length=1, max_length=200)
