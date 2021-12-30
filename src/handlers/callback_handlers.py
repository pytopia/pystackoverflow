import re

import emoji
from src import constants
from src.bot import bot
from src.constants import (inline_keys, keyboards, post_status, post_type,
                           states)
from src.data_models.post import Post
from src.user import User
from src.utils.keyboard import create_keyboard


def register_callback_handlers(self):
    @self.bot.middleware_handler(update_types=['callback_query'])
    def init_callback_handler(bot_instance, call):
        """
        Initialize user to use in other callback handlers.

        1. Get user object.
        2. Demojize call data and call message text.
        """
        # Every message sent with inline keyboard is stored in database with callback_data and
        # post_type (question, answer, comment, ...). When user clicks on an inline keyboard button,
        # we get the post type to know what kind of post we are dealing with.
        call_info = self.get_call_info(call)
        post_id = call_info.get('post_id')
        self.user = User(
            chat_id=call.message.chat.id, first_name=call.message.chat.first_name,
            mongodb=self.db, stackbot=self,
            post_id=post_id
        )

        # register user if not exists
        if not self.user.exists():
            self.user.register(call.message)

        self.user.post.is_gallery = call_info.get('is_gallery', False)

        # Demojize text
        call.data = emoji.demojize(call.data)
        call.message.text = emoji.demojize(call.message.text)

        # update post info
        gallery_filters = self.get_gallery_filters(call.message.chat.id, call.message.message_id)
        self.user.post.gallery_filters = gallery_filters

    @bot.callback_query_handler(func=lambda call: call.data == inline_keys.actions)
    def actions_callback(call):
        """Actions >> inline key callback.
        Post actions include follow/unfollow, answer, comment, open/close, edit, ...

        1. Create actions keyboard according to the current post type.
        2. Get post text content.
        3. Edit message with post text and actions keyboard.
        """
        self.answer_callback_query(call.id, text=call.data)

        # actions keyboard (also update text)
        reply_markup = self.user.post.get_actions_keyboard()

        # TODO: If in future, we update the posts in a queue structure, we can remove this
        text = self.user.post.get_text()

        self.edit_message(call.message.chat.id, call.message.message_id, text=text, reply_markup=reply_markup)

    @bot.callback_query_handler(func=lambda call: call.data in [inline_keys.answer, inline_keys.comment])
    def answer_comment_callback(call):
        """
        Answer/Comment inline key callback.

        1. Update user state.
        2. Store replied to post_id in user tracker for storing the answer/comment when user is done.
            When user sends a reply to a post, there will be replied_to_post_id key stored in the user tracker
            to store the post_id of the post that the user replied to in the answer/comment or any other reply type.
        3. Send start typing message.
        """
        self.answer_callback_query(call.id, text=call.data)

        self.user.update_state(states.ANSWER_QUESTION if call.data == inline_keys.answer else states.COMMENT_POST)
        self.user.track(replied_to_post_id=self.user.post_id)

        current_post_type = post_type.COMMENT if call.data == inline_keys.comment else post_type.ANSWER
        self.user.send_message(
            constants.POST_START_MESSAGE.format(
                first_name=self.user.first_name,
                post_type=current_post_type
            ),
            reply_markup=keyboards.send_post,
        )

    @bot.callback_query_handler(func=lambda call: call.data == inline_keys.back)
    def back_callback(call):
        """
        Back inline key callback.

        1. Check if back is called on a post or on settings.
            - For a post: Edit message with post keyboard.
            - For settings: Edit message with settings keyboard.
        """
        self.answer_callback_query(call.id, text=call.data)

        # main menu keyboard
        if self.user.post_id is not None:
            # back is called on a post (question, answer or comment
            self.edit_message(
                call.message.chat.id, call.message.message_id,
                reply_markup=self.user.post.get_keyboard()
            )
        else:
            # back is called in settings
            self.edit_message(
                call.message.chat.id, call.message.message_id,
                reply_markup=self.get_settings_keyboard()
            )

    @bot.callback_query_handler(
        func=lambda call: call.data in [inline_keys.like, inline_keys.follow, inline_keys.unfollow]
    )
    def toggle_callback(call):
        """
        Toggle callback is used for actions that toggle between pull and push data, such as like, follow, ...

        1. Process callback according to the toggle type.
            - Like: Push/Pull user chat_id from post likes.
            - Follow: Push/Pull user chat_id from post followers.
            - ...
        2. Edit message with new keyboard that toggles based on pull/push.
        """
        self.answer_callback_query(call.id, text=call.data)

        if call.data == inline_keys.like:
            self.user.post.like()
            keyboard = self.user.post.get_keyboard()

        elif call.data in [inline_keys.follow, inline_keys.unfollow]:
            self.user.post.follow()
            keyboard = self.user.post.get_actions_keyboard()

        # update main menu keyboard
        self.edit_message(
            call.message.chat.id, call.message.message_id,
            reply_markup=keyboard
        )

    @bot.callback_query_handler(
        func=lambda call: call.data in [inline_keys.open, inline_keys.close, inline_keys.delete, inline_keys.undelete]
    )
    def toggle_field_values_callback(call):
        """
        Open/Close Delete/Undelete or any other toggling between two values.
        Open means that the post is open for new answers, comments, ...

        1. Open/Close Delete/Undelete post with post_id.
        2. Edit message with new keyboard and text
            - New post text reflects the new open/close status.
        """
        self.answer_callback_query(call.id, text=call.data)

        if call.data in [inline_keys.open, inline_keys.close]:
            field = 'status'
            values = [post_status.OPEN, post_status.CLOSED]
        elif call.data in [inline_keys.delete, inline_keys.undelete]:
            field = 'status'

            # toggle between deleted and current post status
            other_status = self.user.post.post_status
            if other_status == post_status.DELETED:
                other_status = post_status.OPEN
            values = list({post_status.DELETED, other_status})

        self.user.post.toggle_field_values(field=field, values=values)
        self.edit_message(
            call.message.chat.id, call.message.message_id,
            text=self.user.post.get_text(),
            reply_markup=self.user.post.get_actions_keyboard()
        )

    @bot.callback_query_handler(func=lambda call: call.data == inline_keys.change_identity)
    def change_identity_callback(call):
        """
        Change identity inline key callback.

        1. Update settings with change identity keys.
            - User can choose identity between:
                - Anonymous
                - Username
                - First name
        """
        self.answer_callback_query(call.id, text=call.data)

        keyboard = create_keyboard(
            inline_keys.ananymous, inline_keys.first_name, inline_keys.username,
            is_inline=True
        )
        self.edit_message(call.message.chat.id, call.message.message_id, reply_markup=keyboard)

    @bot.callback_query_handler(
        func=lambda call: call.data in [inline_keys.ananymous, inline_keys.first_name, inline_keys.username]
    )
    def set_identity_callback(call):
        """
        Set new user identity.

        1. Update settings with new identity.
        2. Edit message with new settings text and main keyboard.
        """
        self.answer_callback_query(call.id, text=call.data)

        self.user.update_settings(identity_type=call.data)
        self.edit_message(
            call.message.chat.id, call.message.message_id,
            text=self.get_settings_text(), reply_markup=self.get_settings_keyboard()
        )

    @bot.callback_query_handler(func=lambda call: call.data == inline_keys.original_post)
    def original_post(call):
        """
        Original post inline key callback.

        Get the original post from a reply.

        1. Get the current post.
        2. Get the original post from replied_to_post_id.
        3. Edit message with original post keyboard and text.
        4. Update callback data with original post_id.
        """
        self.answer_callback_query(call.id, text=call.data)

        post = self.user.post.as_dict()
        original_post_id = self.db.post.find_one({'_id': post['replied_to_post_id']})['_id']

        post_handler = Post(
            mongodb=self.user.db, stackbot=self.user.stackbot,
            post_id=original_post_id, chat_id=self.user.chat_id,
        )
        self.edit_message(
            call.message.chat.id, call.message.message_id,
            text=post_handler.get_text(),
            reply_markup=post_handler.get_keyboard()
        )

        # we should change the post_id for the buttons
        self.db.callback_data.update_one(
            {'chat_id': call.message.chat.id, 'message_id': call.message.message_id},
            {'$set': {'post_id': original_post_id, 'preview': False, 'is_gallery': False}},
        )

    @bot.callback_query_handler(
        func=lambda call: call.data in [inline_keys.show_comments, inline_keys.show_answers]
    )
    def show_posts(call):
        """
        Show comments and answers of a post.
        """
        self.answer_callback_query(call.id, text=call.data)

        post = self.user.post.as_dict()
        gallery_post_type = post_type.ANSWER if call.data == inline_keys.show_answers else post_type.COMMENT
        gallery_filters = {'replied_to_post_id': post['_id'], 'type': gallery_post_type, 'status': post_status.OPEN}
        posts = self.db.post.find(gallery_filters).sort('date', -1)

        self.db.callback_data.update_one(
            {'chat_id': call.message.chat.id, 'message_id': call.message.message_id},
            {'$set': {'gallery_filters': gallery_filters}},
            upsert=True,
        )

        num_posts = self.db.post.count_documents(gallery_filters)
        next_post = next(posts)

        is_gallery = True if num_posts > 1 else False
        self.edit_gallery(call, next_post['_id'], is_gallery, gallery_filters)

    @bot.callback_query_handler(func=lambda call: call.data in [inline_keys.next_post, inline_keys.prev_post])
    def next_prev_callback(call):
        self.answer_callback_query(call.id, text=call.data)

        post = self.user.post.as_dict()
        operator = '$gt' if call.data == inline_keys.next_post else '$lt'
        asc_desc = 1 if call.data == inline_keys.next_post else -1

        # Get basic filters and gallery filters
        filters = {'date': {operator: post['date']}, 'status': post_status.OPEN}
        gallery_filters = self.db.callback_data.find_one(
            {'chat_id': call.message.chat.id, 'message_id': call.message.message_id}
        )['gallery_filters']
        filters.update(gallery_filters)

        # Get relevant posts
        posts = self.db.post.find(filters).sort('date', asc_desc)

        try:
            next_post = next(posts)
        except StopIteration:
            self.answer_callback_query(call.id, ':red_exclamation_mark: No more posts!')
            return

        is_gallery = True
        self.edit_gallery(call, next_post['_id'], is_gallery, gallery_filters)

    @bot.callback_query_handler(func=lambda call: call.data in [inline_keys.first_page, inline_keys.last_page])
    def gallery_first_last_page(call):
        """
        First and last page of a gallery button.
        """
        self.answer_callback_query(call.id, text=':red_exclamation_mark: No more posts!')

    @bot.callback_query_handler(func=lambda call: re.match(r'[a-zA-Z0-9-]+', call.data))
    def send_file(call):
        """
        Send file callback. Callback data is file_unique_id. We use this to get file from telegram database.
        """
        self.answer_callback_query(call.id, text=f'Sending file: {call.data}...')
        self.send_file(call.message.chat.id, call.data, message_id=call.message.message_id)

    @bot.callback_query_handler(func=lambda call: True)
    def not_implemented_callback(call):
        """
        Raises not implemented callback answer for buttons that are not working yet.
        """
        self.answer_callback_query(call.id, text=f':cross_mark: {call.data} not implemented.')
