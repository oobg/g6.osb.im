from typing_extensions import Annotated, List
from fastapi import Request, Path, Depends
from sqlalchemy import asc, desc, func, select

from core.database import db_session
from core.models import WriteBaseModel
from lib.dependency.dependencies import common_search_query_params
from lib.board_lib import get_list_thumbnail, write_search_filter, get_list, cut_name, is_owner
from service.board_file_service import BoardFileService
from service.ajax import AJAXService
from . import BoardService


class ListPostService(BoardService):
    """
    게시글 목록 클래스
    """

    def __init__(
        self,
        request: Request,
        db: db_session,
        bo_table: Annotated[str, Path(..., title="게시판 테이블명", description="게시판 테이블명")],
        file_service: Annotated[BoardFileService, Depends()],
        search_params: Annotated[dict, Depends(common_search_query_params)],
    ):
        super().__init__(request, db, bo_table)
        if not self.is_list_level():
            self.raise_exception(detail="목록을 볼 권한이 없습니다.", status_code=403)

        self.query = self.get_query(search_params)
        self.file_service = file_service
        self.search_params = search_params
        self.prev_spt = None
        self.next_spt = None

    @classmethod
    async def async_init(
        cls,
        request: Request,
        db: db_session,
        bo_table: Annotated[str, Path(..., title="게시판 테이블명", description="게시판 테이블명")],
        file_service: Annotated[BoardFileService, Depends()],
        search_params: Annotated[dict, Depends(common_search_query_params)],
    ):
        instance = cls(request, db, bo_table, file_service, search_params)
        return instance

    def get_query(self, search_params: dict) -> select:
        """쿼리를 생성합니다."""
        sca = self.request.query_params.get("sca")
        sfl = search_params.get('sfl')
        stx = search_params.get('stx')
        sst = search_params.get('sst')
        sod = search_params.get('sod')

        # 게시글 목록 조회
        self.query = write_search_filter(self.write_model, sca, sfl, stx)

        # 정렬
        if sst and hasattr(self.write_model, sst):
            if sod == "desc":
                self.query = self.query.order_by(desc(sst))
            else:
                self.query = self.query.order_by(asc(sst))
        else:
            self.query = self.get_list_sort_query(self.write_model, self.query)

        if sst and hasattr(self.write_model, sst):
            if sod == "desc":
                self.query = self.query.order_by(desc(sst))
            else:
                self.query = self.query.order_by(asc(sst))
        else:
            self.query = self.get_list_sort_query(self.write_model, self.query)

        if (sca or (sfl and stx)):  # 검색일 경우
            search_part = int(self.config.cf_search_part) or 10000
            min_spt = self.db.scalar(
                select(func.coalesce(func.min(self.write_model.wr_num), 0)))
            spt = int(self.request.query_params.get("spt", min_spt))
            self.prev_spt = spt - search_part if spt > min_spt else None
            self.next_spt = spt + search_part if spt + search_part < 0 else None

            # wr_num 컬럼을 기준으로 검색단위를 구분합니다. (wr_num은 음수)
            self.query = self.query.where(self.write_model.wr_num.between(spt, spt + search_part))

            # 검색 내용에 댓글이 잡히는 경우 부모 글을 가져오기 위해 wr_parent를 불러오는 subquery를 이용합니다.
            subquery = select(self.query.add_columns(self.write_model.wr_parent).distinct().order_by(None).subquery().alias("subquery"))
            self.query = select().where(self.write_model.wr_id.in_(subquery))
        else:   # 검색이 아닌 경우
            self.query = self.query.where(self.write_model.wr_is_comment == 0)

        return self.query

    def add_additional_info_to_writes(
        self,
        writes: List[WriteBaseModel],
        total_count: int,
        offset: int,
        with_files: bool = False,
    ) -> List[WriteBaseModel]:
        """
        게시글 목록에 부가 정보를 추가합니다.
        (댓글, 좋아요, 회원 이미지, 회원 아이콘, 썸네일, 첨부파일)
        """
        ajax_service = AJAXService(self.request, self.db)
        for write in writes:
            write.num = total_count - offset - writes.index(write)
            write = get_list(self.request, self.db, write, self)

            # 댓글 정보를 불러와서 write에 추가합니다.
            comments: List[WriteBaseModel] = self.db.scalars(
            select(self.write_model).filter_by(
                    wr_parent=write.wr_id,
                    wr_is_comment=1
                ).order_by(self.write_model.wr_comment, self.write_model.wr_comment_reply)
            ).all()

            for comment in comments:
                comment.name = cut_name(self.request, comment.wr_name)
                comment.ip = self.get_display_ip(comment.wr_ip)
                comment.is_reply = len(comment.wr_comment_reply) < 5 and self.board.bo_comment_level <= self.member.level
                comment.is_edit = bool(self.member.admin_type or (self.member and comment.mb_id == self.member.mb_id))
                comment.is_del = bool(self.member.admin_type or (self.member and comment.mb_id == self.member.mb_id) or not comment.mb_id)
                comment.is_secret = "secret" in comment.wr_option

                # 회원 이미지, 아이콘 경로 설정
                comment.mb_image_path = self.get_member_image_path(comment.mb_id)
                comment.mb_icon_path = self.get_member_icon_path(comment.mb_id)

                # 비밀댓글 처리
                session_secret_comment_name = f"ss_secret_comment_{self.bo_table}_{comment.wr_id}"
                parent_write = self.db.get(self.write_model, comment.wr_parent)
                if (comment.is_secret
                        and not self.member.admin_type
                        and not is_owner(comment, self.member.mb_id)
                        and not is_owner(parent_write, self.member.mb_id)
                        and not self.request.session.get(session_secret_comment_name)):
                    comment.is_secret_content = True
                    comment.save_content = "비밀글 입니다."
                else:
                    comment.is_secret_content = False
                    comment.save_content = comment.wr_content
            write.comments = comments

            # 게시글 목록 조회시 첨부된 파일을 함께 가져올 경우, default는 False
            if with_files:
                write.images, write.normal_files = self.file_service.get_board_files_by_type(self.bo_table, write.wr_id)

            # 회원 이미지, 아이콘 경로 설정
            write.mb_image_path = self.get_member_image_path(write.mb_id)
            write.mb_icon_path = self.get_member_icon_path(write.mb_id)

            # 게시글 좋아요/싫어요 정보 설정
            ajax_good_data = ajax_service.get_ajax_good_data(self.bo_table, write)
            write.good = ajax_good_data["good"]
            write.nogood = ajax_good_data["nogood"]

            # 게시글 썸네일 설정
            write.thumbnail = get_list_thumbnail(self.request, self.board, write, self.gallery_width, self.gallery_height)

    def get_writes(self, with_files=False, page=1, per_page=None, with_notice=False) -> List[WriteBaseModel]:
        """게시글 목록을 가져옵니다."""
        current_page = page
        if per_page:
            page_rows = per_page        # 페이지당 게시글 수를 별도 설정
        else:
            page_rows = self.page_rows  # 상위 클래스(BoardConfig)에서 설정한 페이지당 게시글 수를 사용

        # with_notice == False -> 공지사항 제외
        if not with_notice:
            notice_ids = self.get_notice_list()
            self.query = self.query.where(self.write_model.wr_id.notin_(notice_ids))

        # 페이지 번호에 따른 offset 계산
        offset = (current_page - 1) * page_rows
        # 최종 쿼리 결과를 가져옵니다.
        writes = self.db.scalars(
            self.query.add_columns(self.write_model)
            .offset(offset).limit(page_rows)
        ).all()

        total_count = self.get_total_count()

        # 게시글 부가 정보 추가 (댓글, 좋아요, 썸네일 등)
        self.add_additional_info_to_writes(writes, total_count, offset, with_files)

        return writes

    def get_notice_writes(self, with_files=False) -> List[WriteBaseModel]:
        """게시글 중 공지사항 목록을 가져옵니다."""
        current_page = self.search_params.get('current_page')
        sca = self.request.query_params.get("sca")
        notice_writes = []
        if current_page == 1:
            notice_ids = self.get_notice_list()
            notice_query = select(self.write_model).where(self.write_model.wr_id.in_(notice_ids))
            if sca:
                notice_query = notice_query.where(self.write_model.ca_name == sca)
            notice_writes = [get_list(self.request, self.db, write, self) for write in self.db.scalars(notice_query).all()]

        # 게시글 부가 정보 추가 (댓글, 좋아요, 썸네일 등)
        self.add_additional_info_to_writes(notice_writes, len(notice_writes), 0, with_files)

        return notice_writes

    def get_total_count(self) -> int:
        """쿼리문을 통해 불러오는 게시글의 수"""
        total_count = self.db.scalar(self.query.add_columns(func.count()).order_by(None))
        return total_count
