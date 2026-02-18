"""
SQLAlchemy models for Island CRUD Admin
"""

from sqlalchemy import Column, String, Text, JSON
from admin.database import Base


class Island(Base):
    """
    Island model - stores island metadata
    Based on the TypeScript island_data.ts structure
    """
    __tablename__ = "islands"

    # Primary key - unique slug identifier
    id = Column(String, primary_key=True, index=True)
    
    # Basic information
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)
    
    # List of available items (stored as JSON array)
    items = Column(JSON, nullable=False, default=[])
    
    # Categorization
    theme = Column(String, nullable=False)  # pink, teal, purple, gold
    cat = Column(String, nullable=False)    # public, member
    
    # Description and details
    description = Column(String, nullable=False)
    seasonal = Column(String, nullable=False)
    
    # Optional map URL
    map_url = Column(String, nullable=True)
    
    def __repr__(self):
        return f"<Island(id='{self.id}', name='{self.name}', type='{self.type}')>"
