from datetime import datetime
from typing_extensions import Annotated, List
from fastapi import Depends, Request, HTTPException
from sqlalchemy import func, select, Select

from core.models import Board, BoardNew
from core.database import db_session
from core.exception import AlertException
from lib.common import dynamic_create_write_table, cut_name, FileCache
from lib.board_lib import BoardConfig, get_list, get_list_thumbnail
from service import BaseService
from service.ajax.ajax import AJAXService
from service.board_file_service import BoardFileService
from service.point_service import PointService
from api.v1.service.member import MemberImageServiceAPI


class BoardNewService(BaseService):
    """
    최신 게시글 관리 클래스(최신 게시글 목록 조회 및 삭제 등)
    """
    def __init__(
        self,
        request: Request,
        db: db_session,
        file_service: Annotated[BoardFileService, Depends()],
        point_service: Annotated[PointService, Depends()],
    ):
        self.request = request
        self.db = db
        self.config = request.state.config
        self.page_rows = self.config.cf_mobile_page_rows if request.state.is_mobile and self.config.cf_mobile_page_rows else self.config.cf_new_rows
        self.file_service = file_service
        self.point_service = point_service

    @classmethod
    async def async_init(
        cls,
        request: Request,
        db: db_session,
        file_service: Annotated[BoardFileService, Depends()],
        point_service: Annotated[PointService, Depends()],
    ):
        instance = cls(request, db, file_service, point_service)
        return instance

    def raise_exception(self, status_code: int, detail: str = None):
        raise AlertException(status_code=status_code, detail=detail)

    def format_datetime(self, wr_datetime: datetime) -> str:
        """
        당일인 경우 시간표시
        """
        current_datetime = datetime.now()

        if wr_datetime.date() == current_datetime.date():
            return wr_datetime.strftime("%H:%M")
        else:
            return wr_datetime.strftime("%y-%m-%d")

    def get_query(
            self, gr_id: str = None, mb_id: str = None, view: str = None
    ) -> Select:
        """검색 조건에 따라 query를 반환"""
        query = select().join(BoardNew.board).order_by(BoardNew.bn_id.desc())

        if gr_id:
            query = query.where(Board.gr_id == gr_id)
        if mb_id:
            query = query.where(BoardNew.mb_id == mb_id)
        if view == "write":
            query = query.where(BoardNew.wr_parent == BoardNew.wr_id)
        elif view == "comment":
            query = query.where(BoardNew.wr_parent != BoardNew.wr_id)
        return query

    def get_offset(self, current_page: int) -> int:
        """페이지 계산을 위한 offset 설정"""
        offset = (current_page - 1) * self.page_rows
        return offset

    def get_board_news(self, query: Select, offset: int, per_page: int = None) -> List[BoardNew]:
        """최신글 목록 조회"""
        per_page = per_page or self.page_rows
        board_news = self.db.scalars(query.add_columns(BoardNew).offset(offset).limit(per_page)).all()
        return board_news

    def get_total_count(self, query: Select) -> int:
        """최신글 총 갯수 조회"""
        total_count = self.db.scalar(query.add_columns(func.count(BoardNew.bn_id)).order_by(None))
        return total_count

    def arrange_borad_news_data(self, board_news: List[BoardNew], total_count: int, offset: int):
        """최신글 결과 데이터 설정"""
        for new in board_news:
            new.num = total_count - offset - (board_news.index((new)))
            # 게시글 정보 조회
            write_model = dynamic_create_write_table(new.bo_table)
            write = self.db.get(write_model, new.wr_id)
            if write:
                # 댓글/게시글 구분
                if write.wr_is_comment:
                    new.subject = "[댓글] " + write.wr_content[:100]
                    new.link = f"/board/{new.bo_table}/{new.wr_parent}#c_{write.wr_id}"
                else:
                    new.subject = write.wr_subject
                    new.link = f"/board/{new.bo_table}/{new.wr_id}"

                # 작성자
                new.name = cut_name(self.request, write.wr_name)
                # 시간설정
                new.datetime = self.format_datetime(write.wr_datetime)

    def get_latest_posts(
        self,
        bo_table_list: List[str], view_type: str = "write",
        rows: int = 10, subject_len: int = 40
    ):
        """최신글 목록 출력"""
        request = self.request
        db = self.db
        boards_info = dict()
        for bo_table in bo_table_list:
            board = db.get(Board, bo_table)
            board_config = BoardConfig(request, board)
            if not board:
                self.raise_exception(
                    status_code=400, detail=f"{bo_table} 게시판 정보가 없습니다."
                )
            board_config = BoardConfig(request, board)
            board.subject = board_config.subject

            #게시글 목록 조회
            write_model = dynamic_create_write_table(bo_table)
            query = select(write_model).order_by(write_model.wr_num).limit(rows)
            if view_type == "comment":
                query = query.where(write_model.wr_is_comment == 1)
            else:
                query = query.where(write_model.wr_is_comment == 0)
            writes = db.scalars(query).all()

            for write in writes:
                write = get_list(request, db, write, board_config, subject_len)
                # 첨부파일 정보 조회
                write.images, write.normal_files = self.file_service.get_board_files_by_type(bo_table, write.wr_id)
                # 썸네일 이미지 설정
                write.thumbnail = get_list_thumbnail(request, board, write, board_config.gallery_width, board_config.gallery_height)

                # 회원 이미지, 아이콘 경로 설정
                write.mb_image_path = MemberImageServiceAPI.get_image_path(write.mb_id)
                write.mb_icon_path = MemberImageServiceAPI.get_icon_path(write.mb_id)

                # 게시글 좋아요/싫어요 정보 설정
                ajax_good_data = AJAXService(self.request, self.db).get_ajax_good_data(bo_table, write)
                write.good = ajax_good_data["good"]
                write.nogood = ajax_good_data["nogood"]

            boards_info[bo_table] = writes

        return boards_info

    def delete_board_news(self, bn_ids: list):
        """최신글 삭제"""
        # 새글 정보 조회
        board_news = self.db.scalars(select(BoardNew).where(BoardNew.bn_id.in_(bn_ids))).all()
        for new in board_news:
            board = self.db.get(Board, new.bo_table)
            write_model = dynamic_create_write_table(new.bo_table)
            write = self.db.get(write_model, new.wr_id)
            if write:
                if write.wr_is_comment == 0:
                    # 게시글 삭제
                    # TODO: 게시글 삭제 공용함수 추가
                    self.db.delete(write)

                    # 원글 포인트 삭제
                    if not self.point_service.delete_point(write.mb_id, board.bo_table, write.wr_id, "쓰기"):
                        self.point_service.save_point(write.mb_id, board.bo_write_point * (-1),
                                                      f"{board.bo_subject} {write.wr_id} 글 삭제")
                else:
                    # 댓글 삭제
                    # TODO: 댓글 삭제 공용함수 추가
                    self.db.delete(write)

                    # 댓글 포인트 삭제
                    if not self.point_service.delete_point(write.mb_id, board.bo_table, write.wr_id, "댓글"):
                        self.point_service.save_point(write.mb_id, board.bo_comment_point * (-1),
                                                      f"{board.bo_subject} {write.wr_parent}-{write.wr_id} 댓글 삭제")
                # 파일 삭제
                self.file_service.delete_board_files(board.bo_table, write.wr_id)

            # 최신글 삭제
            self.db.delete(new)

            # 최신글 캐시 삭제
            FileCache().delete_prefix(f'latest-{new.bo_table}')

        self.db.commit()


class BoardNewServiceAPI(BoardNewService):
    """
    API 요청에 사용되는 최신글 목록 클래스
    - 이 클래스는 API와 관련된 특정 예외 처리를 오버라이드하여 구현합니다.
    """

    def raise_exception(self, status_code: int, detail: str = None):
        raise HTTPException(status_code=status_code, detail=detail)
