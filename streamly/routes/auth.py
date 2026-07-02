from __future__ import annotations
from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from ..config import settings

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory="templates")

@router.get("/site-login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    # Note: SITE_LOGIN_HTML is now in a template file 'login.html'
    return templates.TemplateResponse("login.html", {"request": request, "error": error})

@router.post("/site-login")
async def login_post(request: Request, password: str = Form(...)):
    # In a real app, password should be in config.-SITE_PASSWORD
    if password == "correct_password": # Placeholder
        request.state.session["authenticated"] = True
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid password"})

@router.get("/logout")
async def logout(request: Request):
    request.state.session["authenticated"] = False
    return RedirectResponse(url="/site-login", status_code=303)
