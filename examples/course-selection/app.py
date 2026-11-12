"""选课工程快照的 HTTP 入口，按三轮确认设计保留同一接口。"""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from course_app import CourseApplication, EnrollmentError


app = FastAPI(title='校园选课教学接口')
DATABASE = Path(__file__).with_name('school.sqlite')


class EnrollmentRequest(BaseModel):
    student: str
    offering: str


class EnrollmentResult(BaseModel):
    id: int
    created: bool


@app.post('/enroll', status_code=200, response_model=EnrollmentResult)
def enroll(body: EnrollmentRequest):
    try:
        return CourseApplication(DATABASE).enroll(body.student, body.offering)
    except EnrollmentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
