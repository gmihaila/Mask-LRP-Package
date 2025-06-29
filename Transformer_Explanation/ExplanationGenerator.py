import numpy as np
import torch

# compute rollout between attention layers
def compute_rollout_attention(all_layer_matrices, start_layer=0):
    # adding residual consideration- code adapted from https://github.com/samiraabnar/attention_flow
    num_tokens = all_layer_matrices[0].shape[1]
    batch_size = all_layer_matrices[0].shape[0]
    
    eye = torch.eye(num_tokens).expand(batch_size, num_tokens, num_tokens).to(all_layer_matrices[0].device)
    all_layer_matrices = [all_layer_matrices[i] + eye for i in range(len(all_layer_matrices))]
    matrices_aug = [all_layer_matrices[i] / all_layer_matrices[i].sum(dim=-1, keepdim=True)
                          for i in range(len(all_layer_matrices))]
    joint_attention = matrices_aug[start_layer]
    for i in range(start_layer + 1, len(matrices_aug)):
        joint_attention = matrices_aug[i].bmm(joint_attention)
    return joint_attention.detach().cpu().numpy()


class Generator:
    def __init__(self, model, is_qa=False, is_start=False, model_name='bert'):
        self.model = model
        self.device = model.device
        self.model.eval()
        
        self.is_qa = is_qa
        self.is_start = is_start
        self.model_name = model_name
    

    def MGAE(self, input_ids, attention_mask,
            index=None, start_layer=0, output_attentions=False, 
            head_mask=None, token_type_ids=None, **kwargs):
        
        result = self.model(input_ids=input_ids,
                            attention_mask=attention_mask,
                            token_type_ids=token_type_ids,
                            head_mask=head_mask, 
                            output_attentions=output_attentions, 
                            output_hidden_states=False)

        if self.is_qa:
            output = result['logits']

            one_hot_vector = np.zeros((1, *output[0].size()), dtype=np.float32)
            if self.is_start:
                if index == None:
                    index = np.argmax(output[:, :, 0].cpu().data.numpy(), axis=-1)[0]
                one_hot_vector[0, index, 0] = 1  # start index logit
                pred = output[0][index, 0]
            else:
                if index == None:
                    index = np.argmax(output[:, :, 1].cpu().data.numpy(), axis=-1)[0]
                one_hot_vector[0, index, 1] = 1  # end index logit
                pred = output[0][index, 1]
            
            kwargs = {"alpha": 1, "index": index}

            self.model.zero_grad()
            pred.backward(retain_graph=True)  # chain rule update all parameters in the model

        else:
            kwargs = {"alpha": 1}
            output = result[0]
            
            if index == None:
                index = np.argmax(output.cpu().data.numpy(), axis=-1)

            one_hot = np.zeros((1, output.size()[-1]), dtype=np.float32)
            one_hot[0, index] = 1  # y
            one_hot_vector = one_hot
            one_hot = torch.from_numpy(one_hot).requires_grad_(True)  # fx
            one_hot = torch.sum(one_hot.to(self.model.device) * output)  # sum(y * fx)

            self.model.zero_grad()
            one_hot.backward(retain_graph=True)  # chain rule update all parameters in the model

        self.model.relprop(torch.tensor(one_hot_vector).to(input_ids.device), **kwargs)

        cams = []
        head_contrs = []
        if 'bert' in self.model_name:
            blocks = self.model.__dict__['_modules'][self.model_name].encoder.layer
        elif 'gpt' in self.model_name:
            blocks = self.model.transformer.h
        elif 'llama' in self.model_name:
            blocks = [l.self_attn for l in self.model.model.layers]

        for blk_id, blk in enumerate(blocks):
            # Transformer LRP
            if 'bert' in self.model_name:
                grad = blk.attention.self.get_attn_gradients()
                cam = blk.attention.self.get_attn_cam()
            elif 'gpt' in self.model_name:
                grad = blk.attn.get_attn_gradients()
                cam = blk.attn.get_attn_cam()

            cam = cam[0].reshape(-1, cam.shape[-1], cam.shape[-1])
            grad = grad[0].reshape(-1, grad.shape[-1], grad.shape[-1])
            cam = grad * cam

            head_contr = cam.mean(dim=[1, 2])
            cam = cam.clamp(min=0).mean(dim=0)
            head_contrs.append(head_contr.detach().cpu().numpy())

            cams.append(cam.unsqueeze(0))
            
        rollout = compute_rollout_attention(cams, start_layer=start_layer)
        rollout[:, 0, 0] = 0
        if self.is_qa:
            return rollout[:, index].reshape(-1), head_contrs
        else:
            return rollout[:, 0], head_contrs


    def AttCAT(self, input_ids, attention_mask,
               index=None, start_layer=0, output_attentions=False,
               head_mask=None, token_type_ids=None, **kwargs):

        result = self.model(input_ids=input_ids,
                            attention_mask=attention_mask,
                            token_type_ids=token_type_ids,
                            head_mask=head_mask,
                            output_hidden_states=True)
        if 'bert' in self.model_name:
            blocks = self.model.__dict__['_modules'][self.model_name].encoder.layer
        elif 'gpt' in self.model_name:
            blocks = self.model.transformer.h

        if self.is_qa:
            hs = result.hidden_states
            for blk_id in range(len(blocks)):
                hs[blk_id].retain_grad()
                
            if self.is_start:
                prob = result['logits'][:, :, 0]
            else:
                prob = result['logits'][:, :, 1]

            idx = torch.argmax(prob).cpu().detach().numpy()
            self.model.zero_grad()
            prob[:, idx].backward(retain_graph=True)
        
        else:
            output, hs = result[0], result[1]

            for blk_id in range(len(blocks)):
                hs[blk_id].retain_grad()

            if index == None:
                index = np.argmax(output.cpu().data.numpy(), axis=-1)

            one_hot = np.zeros((1, output.size()[-1]), dtype=np.float32)
            one_hot[0, index] = 1
            one_hot = torch.from_numpy(one_hot).requires_grad_(True)
            one_hot = torch.sum(one_hot.to(input_ids.device) * output)

            self.model.zero_grad()
            one_hot.backward(retain_graph=True)

        cams = {}
        
        for blk_id in range(len(blocks)):
            hs_grads = hs[blk_id].grad
            
            if 'bert' in self.model_name:
                att = blocks[blk_id].attention.self.get_attn().squeeze(0)
            elif 'gpt' in self.model_name:
                att = blocks[blk_id].attn.get_attn().squeeze(0)
            att = att.mean(dim=0)
            att = att.mean(dim=0)
            
            cat = (hs_grads * hs[blk_id]).sum(dim=-1).squeeze(0)
            cat = cat * att
            
            cams[blk_id] = cat
            
        cat_expln = sum(cams.values())

        return cat_expln.detach().cpu().numpy()


    def GAE(self, input_ids, 
            attention_mask,
            index=None, 
            start_layer=0, 
            output_attentions=False,
            token_type_ids=None, **kwargs):
        
        result = self.model(input_ids=input_ids,
                            attention_mask=attention_mask,
                            token_type_ids=token_type_ids,
                            output_attentions=output_attentions, 
                            output_hidden_states=False)

        kwargs = {"alpha": 1}
        output = result[0]
        
        if index == None:
            index = np.argmax(output.cpu().data.numpy(), axis=-1)

        one_hot = np.zeros((1, output.size()[-1]), dtype=np.float32)
        one_hot[0, index] = 1  # y
        one_hot_vector = one_hot
        one_hot = torch.from_numpy(one_hot).requires_grad_(True)  # fx
        one_hot = torch.sum(one_hot.to(self.model.device) * output)  # sum(y * fx)

        self.model.zero_grad()
        one_hot.backward(retain_graph=True)  # chain rule update all parameters in the model

        self.model.relprop(torch.tensor(one_hot_vector).to(input_ids.device), **kwargs)

        cams = []
        head_contrs = []
        if 'bert' in self.model_name:
            blocks = self.model.__dict__['_modules'][self.model_name].encoder.layer
        elif 'gpt' in self.model_name:
            blocks = self.model.transformer.h

        for blk_id, blk in enumerate(blocks):
            # Transformer LRP
            if 'bert' in self.model_name:
                grad = blk.attention.self.get_attn_gradients()
                cam = blk.attention.self.get_attn_cam()
            elif 'gpt' in self.model_name:
                grad = blk.attn.get_attn_gradients()
                cam = blk.attn.get_attn_cam()

            cam = cam[0].reshape(-1, cam.shape[-1], cam.shape[-1])
            grad = grad[0].reshape(-1, grad.shape[-1], grad.shape[-1])
            cam = grad * cam

            head_contr = cam.mean(dim=[1, 2])
            cam = cam.clamp(min=0).mean(dim=0)
            head_contrs.append(head_contr.detach().cpu().numpy())

            cams.append(cam.unsqueeze(0))
            
        rollout = compute_rollout_attention(cams, start_layer=start_layer)
        rollout[:, 0, 0] = 0
        if self.is_qa:
            return rollout[:, index].reshape(-1), head_contrs
        else:
            return rollout[:, 0], head_contrs


    def generate_LRP_last_layer(self, input_ids, attention_mask,
                                index=None, start_layer=0, output_attentions=False, 
                                head_mask=None, token_type_ids=None, **kwargs):
        output = self.model(input_ids=input_ids, 
                            attention_mask=attention_mask, 
                            token_type_ids=token_type_ids,
                            head_mask=head_mask, 
                            output_attentions=output_attentions, 
                            output_hidden_states=True)[0]
        kwargs = {"alpha": 1}
        if index == None:
            index = np.argmax(output.cpu().data.numpy(), axis=-1)

        one_hot = np.zeros((1, output.size()[-1]), dtype=np.float32)
        one_hot[0, index] = 1
        one_hot_vector = one_hot
        one_hot = torch.from_numpy(one_hot).requires_grad_(True)
        one_hot = torch.sum(one_hot.to(self.model.device) * output)

        self.model.zero_grad()
        one_hot.backward(retain_graph=True)

        self.model.relprop(torch.tensor(one_hot_vector).to(input_ids.device), **kwargs)

        if 'bert' in self.model_name:
            cam = self.model.__dict__['_modules'][self.model_name].encoder.layer[-1].attention.self.get_attn_cam()[0]
        else:
            cam = self.model.transformer.h[-1].attn.get_attn_cam()[0]
        cam = cam.clamp(min=0).mean(dim=0).unsqueeze(0)
        cam[:, 0, 0] = 0
        return cam[:, 0].detach().cpu().numpy(), None


    def generate_full_lrp(self, input_ids, attention_mask,
                          index=None, start_layer=0, output_attentions=False, 
                          head_mask=None, token_type_ids=None, **kwargs):
        output = self.model(input_ids=input_ids, 
                            attention_mask=attention_mask, 
                            token_type_ids=token_type_ids,
                            head_mask=head_mask, 
                            output_attentions=output_attentions, 
                            output_hidden_states=True)[0]
        kwargs = {"alpha": 1}

        if index == None:
            index = np.argmax(output.cpu().data.numpy(), axis=-1)

        one_hot = np.zeros((1, output.size()[-1]), dtype=np.float32)
        one_hot[0, index] = 1
        one_hot_vector = one_hot
        one_hot = torch.from_numpy(one_hot).requires_grad_(True)
        one_hot = torch.sum(one_hot.to(self.model.device) * output)

        self.model.zero_grad()
        one_hot.backward(retain_graph=True)

        cam = self.model.relprop(torch.tensor(one_hot_vector).to(input_ids.device), **kwargs)
        cam = cam.sum(dim=2)
        cam[:, 0] = 0
        return cam.detach().cpu().numpy(), None


    def generate_attn_last_layer(self, input_ids, attention_mask,
                                 index=None, start_layer=0, output_attentions=False, 
                                 head_mask=None, token_type_ids=None, **kwargs):
        self.model(input_ids=input_ids, attention_mask=attention_mask, token_type_ids=token_type_ids)[0]
        if 'bert' in self.model_name:
            cam = self.model.__dict__['_modules'][self.model_name].encoder.layer[-1].attention.self.get_attn()[0]
        else:
            cam = self.model.transformer.h[-1].attn.get_attn()[0]
        cam = cam.mean(dim=0).unsqueeze(0)
        cam[:, 0, 0] = 0

        if self.is_qa:
            return torch.mean(cam, dim=[0, 1]).detach().cpu().numpy(), None
        else:
            return cam[:, 0].detach().cpu().numpy(), None


    def generate_rollout(self, input_ids, attention_mask,
                         index=None, start_layer=0, output_attentions=False, 
                         head_mask=None, token_type_ids=None, **kwargs):
        self.model.zero_grad()
        output = self.model(input_ids=input_ids, 
                            attention_mask=attention_mask, 
                            token_type_ids=token_type_ids)[0]
        if 'bert' in self.model_name:
            blocks = self.model.__dict__['_modules'][self.model_name].encoder.layer
        elif 'gpt' in self.model_name:
            blocks = self.model.transformer.h
        all_layer_attentions = []
        for blk in blocks:
            if 'bert' in self.model_name:
                attn_heads = blk.attention.self.get_attn()
            elif 'gpt' in self.model_name:
                attn_heads = blk.attn.get_attn()
            avg_heads = (attn_heads.sum(dim=1) / attn_heads.shape[1]).detach()
            all_layer_attentions.append(avg_heads)
        rollout = compute_rollout_attention(all_layer_attentions, start_layer=start_layer)
        rollout[:, 0, 0] = 0
        if self.is_qa:
            return np.mean(rollout, axis=(0, 1)), None
        else:
            return rollout[:, 0], None


    def generate_attn_gradcam(self, input_ids, attention_mask,
                              index=None, start_layer=0, output_attentions=False, 
                              head_mask=None, token_type_ids=None, **kwargs):
        
        output = self.model(input_ids=input_ids, 
                            attention_mask=attention_mask, 
                            token_type_ids=token_type_ids)[0]
        kwargs = {"alpha": 1}

        if index == None:
            index = np.argmax(output.cpu().data.numpy(), axis=-1)

        one_hot = np.zeros((1, output.size()[-1]), dtype=np.float32)
        one_hot[0, index] = 1
        one_hot_vector = one_hot
        one_hot = torch.from_numpy(one_hot).requires_grad_(True)
        one_hot = torch.sum(one_hot.to(self.model.device) * output)

        self.model.zero_grad()
        one_hot.backward(retain_graph=True)

        self.model.relprop(torch.tensor(one_hot_vector).to(input_ids.device), **kwargs)

        if 'bert' in self.model_name:
            grad = self.model.__dict__['_modules'][self.model_name].encoder.layer[-1].attention.self.get_attn_gradients()
            cam = self.model.__dict__['_modules'][self.model_name].encoder.layer[-1].attention.self.get_attn()
        elif 'gpt' in self.model_name:
            grad = self.model.transformer.h.layer[-1].attn.get_attn_gradients()
            cam = self.model.transformer.h.layer[-1].attn.get_attn_cam()

        cam = cam[0].reshape(-1, cam.shape[-1], cam.shape[-1])
        grad = grad[0].reshape(-1, grad.shape[-1], grad.shape[-1])
        grad = grad.mean(dim=[1, 2], keepdim=True)
        cam = (cam * grad).mean(0).clamp(min=0).unsqueeze(0)
        cam = (cam - cam.min()) / (cam.max() - cam.min())
        cam[:, 0, 0] = 0

        return cam[:, 0].detach().cpu().numpy(), None


    def generate_gradcam(self, input_ids, attention_mask,
                         index=None, start_layer=0, output_attentions=False, 
                         head_mask=None, token_type_ids=None, **kwargs):
        output = self.model(input_ids=input_ids, 
                            attention_mask=attention_mask, 
                            token_type_ids=token_type_ids)[0]
        kwargs = {"alpha": 1}

        if index == None:
            index = np.argmax(output.cpu().data.numpy(), axis=-1)

        one_hot = np.zeros((1, output.size()[-1]), dtype=np.float32)
        one_hot[0, index] = 1
        one_hot_vector = one_hot
        one_hot = torch.from_numpy(one_hot).requires_grad_(True)
        one_hot = torch.sum(one_hot.to(input_ids.device) * output)

        self.model.zero_grad()
        one_hot.backward(retain_graph=True)

        self.model.relprop(torch.tensor(one_hot_vector).to(input_ids.device), **kwargs)
        if 'bert' in self.model_name:
            grad = self.model.__dict__['_modules'][self.model_name].encoder.layer[-1].attention.self.get_attn_gradients()
        elif 'gpt' in self.model_name:
            grad = self.model.transformer.h.layer[-1].attn.get_attn_gradients()

        grad = grad[0].reshape(-1, grad.shape[-1], grad.shape[-1])
        grad = grad.mean(dim=[0, 1], keepdim=True).clamp(min=0).squeeze(0)
        grad[:, 0] = 0
        
        return grad.detach().cpu().numpy(), None
