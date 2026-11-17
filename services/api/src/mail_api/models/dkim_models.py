"""
Pydantic models for RevBridge DKIM

Data models for DKIM identities and keys, including:
- DKIM identity settings
- DKIM key management
- Responses and filters
"""

from datetime import datetime

from pydantic import BaseModel, Field, model_validator, validator


class DkimIdentityCreate(BaseModel):
    """Model to create a DKIM identity"""

    cluster_id: str = Field(..., min_length=1, max_length=255, description="Cluster ID")
    from_email: str | None = Field(None, max_length=255, description="Sender email")
    from_domain: str | None = Field(None, max_length=255, description="Sender domain")
    wildcard_subdomains: bool = Field(False, description="Whether wildcard subdomains are accepted")
    selector: str = Field(..., min_length=1, max_length=255, description="DKIM selector")
    dkim_domain: str = Field(..., min_length=1, max_length=255, description="DKIM domain")

    @model_validator(mode='after')
    def validate_email_or_domain(self):
        """Requires at least from_email or from_domain"""
        from_email = self.from_email
        from_domain = self.from_domain
        if not from_email and not from_domain:
            raise ValueError("Provide at least from_email or from_domain")
        return self


class DkimIdentityUpdate(BaseModel):
    """Model to update a DKIM identity"""

    cluster_id: str | None = Field(None, min_length=1, max_length=255)
    from_email: str | None = Field(None, max_length=255)
    from_domain: str | None = Field(None, max_length=255)
    wildcard_subdomains: bool | None = None
    selector: str | None = Field(None, min_length=1, max_length=255)
    dkim_domain: str | None = Field(None, min_length=1, max_length=255)


class DkimIdentityResponse(BaseModel):
    """DKIM identity response model"""

    id: int
    cluster_id: str
    from_email: str | None
    from_domain: str | None
    wildcard_subdomains: bool
    selector: str
    dkim_domain: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DkimIdentityFilter(BaseModel):
    """DKIM identity search filters"""

    cluster_id: str | None = None
    from_email: str | None = None
    from_domain: str | None = None
    selector: str | None = None
    dkim_domain: str | None = None


class DkimKeyCreate(BaseModel):
    """Model to create a DKIM key.

    Three modes:
      * Path (``private_key_path``): the key was generated outside the API (for
        example with opendkim-genkey) and the API only records its path.
      * Generate (``generate_key=true``): the API generates the RSA key pair, stores
        ``private_key_pem`` in the database (inline signing, no key file) and returns
        the ``public_key`` (the ``p=`` value of the DKIM TXT record) so it can be
        published in DNS.
      * Import (``private_key_pem`` given): the caller passes the private key the
        domain ALREADY uses. The API stores it keeping the SAME selector, so the
        signature and DNS do not change (a replacement, not a rotation). If the key
        already exists, ``private_key_pem`` is backfilled.
    """

    cluster_id: str = Field(..., min_length=1, max_length=255, description="Cluster ID")
    selector: str = Field(..., min_length=1, max_length=255, description="DKIM selector")
    dkim_domain: str = Field(..., min_length=1, max_length=255, description="DKIM domain")
    private_key_path: str | None = Field(
        None, max_length=512, description="Path of the private key (path mode)"
    )
    private_key_pem: str | None = Field(
        None, description="Existing private key to import (import mode)"
    )
    generate_key: bool = Field(
        False,
        description="If true, the API generates the RSA key pair, stores the PEM and returns the public_key",
    )
    key_size: int = Field(2048, ge=1024, le=4096, description="RSA key size (only with generate_key)")

    @validator("private_key_path")
    def validate_key_path(cls, v):
        """Normalizes the key path when given"""
        if v is not None and not v.strip():
            raise ValueError("The private key path cannot be empty")
        return v.strip() if v else v

    @model_validator(mode="after")
    def validate_path_or_generate(self):
        """Requires a path, an import (PEM) OR server-side generation."""
        if not self.private_key_path and not self.generate_key and not self.private_key_pem:
            raise ValueError("Provide private_key_path, private_key_pem (import) or generate_key=true")
        return self


class DkimKeyUpdate(BaseModel):
    """Model to update a DKIM key"""

    cluster_id: str | None = Field(None, min_length=1, max_length=255)
    selector: str | None = Field(None, min_length=1, max_length=255)
    dkim_domain: str | None = Field(None, min_length=1, max_length=255)
    private_key_path: str | None = Field(None, min_length=1, max_length=512)
    private_key_pem: str | None = Field(
        None, description="Import/update the stored private key (import/backfill)"
    )


class DkimKeyResponse(BaseModel):
    """DKIM key response model.

    The private key is NEVER returned. ``public_key`` (the ``p=`` value of the DKIM TXT
    record) is only filled in the creation response with ``generate_key=true``, so the
    record can be published in DNS.
    """

    id: int
    cluster_id: str
    selector: str
    dkim_domain: str
    private_key_path: str
    public_key: str | None = Field(
        None, description="p= value of the DKIM TXT record (only on creation with generate_key)"
    )
    dns_record_name: str | None = Field(
        None, description="Name of the DKIM TXT record: <selector>._domainkey.<dkim_domain>"
    )
    dns_record_value: str | None = Field(
        None, description="Full value of the DKIM TXT record: v=DKIM1; k=rsa; p=..."
    )
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DkimKeyFilter(BaseModel):
    """DKIM key search filters"""

    cluster_id: str | None = None
    selector: str | None = None
    dkim_domain: str | None = None
