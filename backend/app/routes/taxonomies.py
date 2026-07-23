import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import require_editor_or_admin, require_viewer_or_above
from app.database import get_db
from app.models import Category, CanonicalProduct, User
from app.schemas import CategoryCreate, CategoryOut, CategoryUpdate
from app.services.deduplication import normalize_text


router = APIRouter(prefix="/settings/categories", tags=["Taxonomy Settings"])


def _category_output(db: Session, category: Category) -> CategoryOut:
    product_count = (
        db.query(func.count(CanonicalProduct.id))
        .filter(
            CanonicalProduct.category_id == category.id,
            CanonicalProduct.is_deleted == False,
        )
        .scalar()
    ) or 0
    return CategoryOut(
        id=category.id,
        name=category.name,
        parent_id=category.parent_id,
        level=category.level,
        path=category.path,
        product_count=product_count,
    )


@router.get("", response_model=List[CategoryOut])
def list_categories(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_viewer_or_above),
):
    categories = db.query(Category).order_by(Category.path.asc()).all()
    return [_category_output(db, category) for category in categories]


@router.post("", response_model=CategoryOut, status_code=status.HTTP_201_CREATED)
def create_category(
    payload: CategoryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin),
):
    name = " ".join(payload.name.split())
    if not normalize_text(name):
        raise HTTPException(status_code=400, detail="Category name cannot be blank.")

    parent = None
    if payload.parent_id:
        parent = db.query(Category).filter(Category.id == payload.parent_id).first()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent category not found.")

    path = f"{parent.path} > {name}" if parent else name
    if db.query(Category).filter(func.lower(Category.path) == path.lower()).first():
        raise HTTPException(status_code=409, detail="That taxonomy path already exists.")

    category = Category(
        id=uuid.uuid4(),
        name=name,
        parent_id=parent.id if parent else None,
        level=(parent.level + 1) if parent else 0,
        path=path,
    )
    db.add(category)
    db.commit()
    db.refresh(category)
    return _category_output(db, category)


@router.put("/{category_id}", response_model=CategoryOut)
def rename_category(
    category_id: uuid.UUID,
    payload: CategoryUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin),
):
    category = db.query(Category).filter(Category.id == category_id).first()
    if not category:
        raise HTTPException(status_code=404, detail="Category not found.")

    name = " ".join(payload.name.split())
    if not normalize_text(name):
        raise HTTPException(status_code=400, detail="Category name cannot be blank.")

    old_path = category.path
    parent = db.query(Category).filter(Category.id == category.parent_id).first() if category.parent_id else None
    new_path = f"{parent.path} > {name}" if parent else name
    duplicate = db.query(Category).filter(
        func.lower(Category.path) == new_path.lower(),
        Category.id != category.id,
    ).first()
    if duplicate:
        raise HTTPException(status_code=409, detail="That taxonomy path already exists.")

    descendants = db.query(Category).filter(Category.path.like(f"{old_path} > %")).all()
    category.name = name
    category.path = new_path
    for child in descendants:
        child.path = f"{new_path}{child.path[len(old_path):]}"
    db.commit()
    db.refresh(category)
    return _category_output(db, category)


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(
    category_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin),
):
    category = db.query(Category).filter(Category.id == category_id).first()
    if not category:
        raise HTTPException(status_code=404, detail="Category not found.")
    if db.query(Category).filter(Category.parent_id == category.id).first():
        raise HTTPException(status_code=409, detail="Delete or move child categories first.")
    if db.query(CanonicalProduct).filter(
        CanonicalProduct.category_id == category.id,
        CanonicalProduct.is_deleted == False,
    ).first():
        raise HTTPException(status_code=409, detail="Category is assigned to products and cannot be deleted.")
    db.delete(category)
    db.commit()
    return None
